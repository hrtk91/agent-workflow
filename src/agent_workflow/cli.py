from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_workflow.analytics import AnalyticsReporter, render_run_detail, render_text_report
from agent_workflow.config import (
    CONFIG_FILE_ENV,
    default_config_path,
    initialize_settings,
    load_settings,
    render_settings,
)
from agent_workflow.merge import MergeBlocked, MergeGateConfig, run_merge_approved, run_merge_gate
from agent_workflow.tui import run_tui
from agent_workflow.repair import REPAIR_ACTIONS, REPAIR_CATEGORIES, REPAIR_RISKS, RepairDraftInput, RepairManager
from agent_workflow.runner import (
    AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS,
    AutoRepairConfig,
    FAILURE_NOTIFY_STATUSES,
    RunnerConfig,
    WorkflowRunner,
    default_state_dir,
)
from agent_workflow.telemetry import export_report_to_otel


class JapaneseArgumentParser(argparse.ArgumentParser):
    """argparseの自動生成部分も日本語で表示するパーサー。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置引数"
        self._optionals.title = "オプション"
        for action in self._actions:
            if action.dest == "help":
                action.help = "このヘルプを表示して終了する"


def build_parser() -> argparse.ArgumentParser:
    parser = JapaneseArgumentParser(prog="aw", description="ローカルのエージェントワークフローを中断後も再開できる軽量ランナー")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(), help="run状態を保存するディレクトリ")
    parser.add_argument("--config-file", type=Path, default=default_config_path(), help="使用する設定ファイル")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="新しいタスク実行を開始する")
    add_run_args(run)

    enqueue = sub.add_parser("enqueue", help="タスク実行をキューに追加してすぐ戻る")
    add_run_args(enqueue)

    tick = sub.add_parser("tick", help="キューのジョブを1回実行して終了する")
    tick.add_argument("--max-runs", type=int, default=1, help="1回のtickで実行するジョブ数")
    tick.add_argument(
        "--isolate-job-failures",
        action="store_true",
        help="キューのジョブが失敗してもawが状態を記録できれば成功終了する（通知またはaw基盤の失敗は失敗終了）",
    )
    add_notify_args(tick)
    add_auto_repair_args(tick)

    worker = sub.add_parser("worker", help="キューのジョブを繰り返し実行する")
    worker.add_argument("--interval-seconds", type=float, default=60, help="ジョブがないときのtick間隔（秒）")
    worker.add_argument("--max-runs-per-tick", type=int, default=1, help="1回のtickで実行するジョブ数")
    worker.add_argument("--parallelism", type=int, default=1, help="同時に実行するclaim済みジョブの最大数")
    worker.add_argument("--repo-parallelism", type=int, default=1, help="リポジトリごとの同時実行数（0で制限なし）")
    worker.add_argument("--inline", action="store_true", help="子プロセスではなくworkerプロセス内でジョブを実行する")
    worker.add_argument("--no-recover-stale-running", action="store_true", help="worker起動時に既存のrunningジョブを失敗扱いにしない")
    worker.add_argument("--stop-when-idle", action="store_true", help=argparse.SUPPRESS)
    add_notify_args(worker)
    add_auto_repair_args(worker)

    run_claimed = sub.add_parser("run-claimed", help="内部用のclaim済みジョブを実行する")
    run_claimed.add_argument("--job-id", required=True)
    add_notify_args(run_claimed)
    add_auto_repair_args(run_claimed)

    resume = sub.add_parser("resume", help="失敗または中断したrunを再開する")
    resume.add_argument("--run-id", required=True, help="再開するrunのID")
    resume.add_argument("--verify-command", help="QCコマンドを上書きする")
    resume.add_argument("--timeout-seconds", type=float, help="タイムアウト秒数を上書きする")
    add_notify_args(resume)

    retry = sub.add_parser("retry", help="指定したstepと後続stepを再試行する")
    retry.add_argument("--run-id", required=True, help="再試行するrunのID")
    retry.add_argument("--step", required=True, help="再試行するstep名")
    retry.add_argument("--verify-command", help="QCコマンドを上書きする")
    retry.add_argument("--timeout-seconds", type=float, help="タイムアウト秒数を上書きする")
    add_notify_args(retry)

    status = sub.add_parser("status", help="runの状態を表示する")
    status.add_argument("--run-id", help="指定したrunだけを表示する")
    status.add_argument("--include-repair", action="store_true", help="内部の修復診断ジョブを最近の状態表示に含める")

    ui = sub.add_parser("ui", aliases=["tui"], help="パイプラインをTUIで監視する")
    ui.add_argument("--refresh-seconds", type=float, default=1.0, help="画面を更新する間隔（秒）")
    ui.add_argument("--include-repair", action="store_true", help="repairとrepair-actionのrunを含める")

    report = sub.add_parser("report", help="SQLiteからQC結果とrunメトリクスを集計する")
    report.add_argument("--run-id", help="1つのrunの現在のstepと全試行履歴を表示する")
    report.add_argument("--group-by", default="model", help="カンマ区切りの集計軸: model, provider, task_type, workflow, repo, status")
    report.add_argument("--repo", type=Path, help="リポジトリパスで絞り込む")
    report.add_argument("--since", help="このISO日時以降に作成されたrunを含める")
    report.add_argument("--format", choices=["text", "json"], default="text", help="出力形式（textまたはjson）")
    report.add_argument("--export-otel", action="store_true", help="集計レポートをOTLP/HTTP gaugeとしてエクスポートする")
    report.add_argument("--include-repair", action="store_true", help="repairとrepair-actionのrunを含める")

    summary = sub.add_parser("summary", help="summaryのパスを表示する")
    summary.add_argument("--run-id", required=True, help="表示するrunのID")
    summary.add_argument("--discord", action="store_true", help="Hermes向けDiscord summaryのパスを表示する")

    cleanup = sub.add_parser("cleanup", help="runのworktreeを削除する")
    cleanup.add_argument("--run-id", required=True, help="worktreeを削除するrunのID")

    config = sub.add_parser("config", help="agent-workflowの設定を確認・初期化する")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="現在使用している設定ファイルのパスを表示する")
    config_sub.add_parser("show", help="有効な設定をTOMLで表示する")
    config_init = config_sub.add_parser("init", help="デフォルト設定ファイルを書き出す")
    config_init.add_argument("--force", action="store_true")

    repair = sub.add_parser("repair", help="ワークフロー修復draftを作成・検証する")
    repair_sub = repair.add_subparsers(dest="repair_command", required=True)
    repair_draft = repair_sub.add_parser("draft", help="検証済みの修復draft成果物を書き出す")
    repair_draft.add_argument("--failed-run-id", required=True, help="失敗したrunのID")
    repair_draft.add_argument("--title", required=True, help="修復draftのタイトル")
    repair_draft.add_argument("--category", choices=sorted(REPAIR_CATEGORIES), required=True, help="修復の分類")
    repair_draft.add_argument("--risk", choices=sorted(REPAIR_RISKS), required=True, help="修復のリスク")
    repair_draft.add_argument("--proposed-action", choices=sorted(REPAIR_ACTIONS), required=True, help="提案する修復アクション")
    repair_draft.add_argument("--diagnosis-file", type=Path, required=True, help="診断Markdownファイル")
    repair_draft.add_argument("--evidence-file", type=Path, required=True, help="証拠Markdownファイル")
    repair_draft.add_argument("--notify-before-file", type=Path, required=True, help="実行前通知Markdownファイル")
    repair_draft.add_argument("--verify-command", help="修復後の検証コマンド")
    repair_draft.add_argument("--retry-original", action="store_true", help="元のrunを再試行する修復として扱う")
    repair_draft.add_argument("--environment", help="修復対象の環境")
    repair_draft.add_argument("--healthcheck-command", help="修復後のhealthcheckコマンド")
    repair_draft.add_argument("--rollback-plan-file", type=Path, help="ロールバック計画Markdownファイル")
    repair_draft.add_argument("--allow-duplicate", action="store_true", help="重複するdraftの作成を許可する")

    repair_validate = repair_sub.add_parser("validate", help="既存の修復draftを検証する")
    target = repair_validate.add_mutually_exclusive_group(required=True)
    target.add_argument("--draft-id", help="検証するdraftのID")
    target.add_argument("--draft-dir", type=Path, help="検証するdraftディレクトリ")

    watchdog = sub.add_parser("watchdog", help="修復の切り分けが必要なワークフロー失敗を確認する")
    watchdog_sub = watchdog.add_subparsers(dest="watchdog_command", required=True)
    watchdog_scan = watchdog_sub.add_parser("scan", help="修復draftがない失敗runを一覧表示する")
    watchdog_scan.add_argument("--limit", type=int, default=20, help="表示する件数")
    watchdog_scan.add_argument("--include-repaired", action="store_true", help="修復済みのrunも含める")

    merge_gate = sub.add_parser("merge-gate", help="GitHub PRをマージできるか評価する")
    merge_gate.add_argument("--repo", required=True, help="GitHubリポジトリのslug（例: owner/name）")
    merge_gate.add_argument("--pr", type=int, required=True, help="プルリクエスト番号")
    merge_gate.add_argument("--repo-path", type=Path, help="ローカルQC用worktreeを作るgitリポジトリ")
    merge_gate.add_argument("--verify-command", help="PR headを分離したworktreeで実行するローカルQCコマンド")
    merge_gate.add_argument("--timeout-seconds", type=float, default=7200, help="ローカルQCのタイムアウト秒数")
    merge_gate.add_argument("--base-branch", default="main", help="比較するbaseブランチ")
    merge_gate.add_argument("--output-dir", type=Path, help="decision成果物の出力先")
    merge_gate.add_argument("--allow-no-checks", action="store_true", help="GitHub checksもローカルQCもない場合の承認を許可する")
    merge_gate.add_argument("--keep-worktree", action="store_true")

    merge = sub.add_parser("merge", help="MERGE_APPROVEDのdecisionファイルを使ってPRをマージする")
    merge.add_argument("--decision", type=Path, required=True, help="MERGE_APPROVEDのdecisionファイル")
    merge.add_argument("--execute", action="store_true", help="gh pr mergeを実行する（デフォルトはdry-run）")
    merge.add_argument("--max-age-seconds", type=int, default=int(os.environ.get("HERMES_MERGE_DECISION_MAX_AGE_SECONDS", "86400")), help="decisionを有効とする最大経過秒数")
    merge.add_argument("--method", choices=["merge", "squash", "rebase"], default="squash", help="GitHubで使うマージ方法")
    merge.add_argument("--keep-branch", action="store_true", help="マージ後もブランチを削除しない")
    merge.add_argument("--repo-path", type=Path, help="live PRにchecksがない場合にQCを再実行するローカルgitリポジトリ")
    merge.add_argument("--verify-command", help="checksがない場合にlive PR headで再実行するローカルQCコマンド")
    merge.add_argument("--timeout-seconds", type=float, default=7200, help="ローカルQCのタイムアウト秒数")
    merge.add_argument("--allow-no-checks", action="store_true", help="GitHub checksがないlive PRを明示的に許可し、ローカルQCの再確認を省略する")

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
    parser.add_argument("--task-type", default="unspecified", help="分析レポートで使うタスク分類")
    parser.add_argument("--base-ref")
    parser.add_argument("--keep-worktree", action="store_true", default=True)
    add_notify_args(parser)


def add_notify_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--notify-command",
        help="終端状態になったrunの後に実行するshellコマンドのテンプレート。プレースホルダー: {job_id} {run_id} {status} {summary} {discord_summary}",
    )
    parser.add_argument(
        "--notify-statuses",
        default=",".join(sorted(FAILURE_NOTIFY_STATUSES)),
        help="通知するstatusをカンマ区切りで指定するか'all'を指定する。デフォルトは失敗系statusのみ",
    )


def add_auto_repair_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auto-repair",
        action="store_true",
        help="通常のワークフローrunが終端失敗したとき、修復診断ジョブをキューへ追加する",
    )
    parser.add_argument("--repair-workflow", default="default", help="auto-repair診断ジョブで使うworkflow")
    parser.add_argument("--repair-verify-command", help="auto-repair診断ジョブの検証コマンド")
    parser.add_argument("--repair-timeout-seconds", type=float, help="auto-repair診断ジョブのタイムアウト")
    parser.add_argument("--repair-executor-bin", help="auto-repair診断ジョブで使うexecutor binary")
    parser.add_argument("--repair-provider", help="auto-repair診断ジョブで使うexecutor provider")
    parser.add_argument("--repair-model", help="auto-repair診断ジョブで使うexecutor model")
    parser.add_argument(
        "--repair-scan-existing",
        action="store_true",
        help="既存の失敗runも補完する。通知の大量発生を避けるため、デフォルトでは無効",
    )
    parser.add_argument(
        "--repair-scan-existing-max-age-seconds",
        type=float,
        default=AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS,
        help=f"この秒数以内に更新された失敗runだけを補完する。デフォルト: {AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS}",
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
        task_type=getattr(args, "task_type", "unspecified"),
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
    config_path = args.config_file.expanduser().absolute()
    os.environ[CONFIG_FILE_ENV] = str(config_path)

    try:
        if args.command == "config":
            if args.config_command == "path":
                print(config_path)
                return 0
            if args.config_command == "show":
                print(render_settings(load_settings(config_path)), end="")
                return 0
            if args.config_command == "init":
                print(initialize_settings(config_path, force=args.force))
                return 0
        if args.command == "report":
            # report 処理フロー:
            # [1] SQLiteをread-onlyで開き、run詳細または集計payloadを読み取る。
            # [2] 集計時だけ、明示指定に応じて同じpayloadをOTLP metricsへ送信する。
            # [3] JSONまたはterminal向けtextとして標準出力へ表示する。
            # [1] runnerを初期化せず、report専用のread-only query経路を使う。
            analytics = AnalyticsReporter(args.state_dir.expanduser() / "jobs.sqlite")
            if args.run_id:
                if args.export_otel:
                    raise ValueError("--export-otel cannot be used with --run-id")
                report_data = analytics.run_detail(args.run_id)
                print(
                    json.dumps(report_data, indent=2, sort_keys=True)
                    if args.format == "json"
                    else render_run_detail(report_data)
                )
                return 0
            group_by = [field.strip() for field in args.group_by.split(",") if field.strip()]
            repo_path = str(args.repo.expanduser().resolve()) if args.repo else None
            report_data = analytics.report(
                group_by=group_by,
                repo_path=repo_path,
                since=args.since,
                include_repair=args.include_repair,
            )
            # [2] SQLiteから読み取った集計結果を、明示指定された場合だけ外部へ投影する。
            if args.export_otel:
                export_report_to_otel(report_data)
            # [3] textとJSONのどちらでも同じreport payloadを表示する。
            if args.format == "json":
                print(json.dumps(report_data, indent=2, sort_keys=True))
            else:
                print(render_text_report(report_data))
            return 0
        if args.command in {"ui", "tui"}:
            run_tui(
                args.state_dir,
                refresh_seconds=args.refresh_seconds,
                include_repair=args.include_repair,
            )
            return 0
        runner = WorkflowRunner(args.state_dir)
        if args.command == "run":
            state = runner.run_new(config_from_args(args))
            notify_error = notify_result_if_requested(runner, state, args)
            if notify_error:
                print(f"agent-workflow: 通知に失敗しました: {notify_error}", file=sys.stderr)
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
                print(f"agent-workflow: 通知に失敗しました: {notify_error}", file=sys.stderr)
            print(state.summary_path)
            return run_exit_code(state.status, notify_error)
        if args.command == "retry":
            state = runner.retry(args.run_id, args.step, verify_command=args.verify_command, timeout_seconds=args.timeout_seconds)
            notify_error = notify_result_if_requested(runner, state, args)
            if notify_error:
                print(f"agent-workflow: 通知に失敗しました: {notify_error}", file=sys.stderr)
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
                    repo_path=args.repo_path,
                    verify_command=args.verify_command,
                    timeout_seconds=args.timeout_seconds,
                )
            )
            return 0
    except MergeBlocked as exc:
        print(f"agent-workflow: マージがブロックされました: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"agent-workflow: {exc}", file=sys.stderr)
        return 2
    return 2
