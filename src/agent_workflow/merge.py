from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_workflow.runner import new_run_id, run_logged, utc_now

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UNACCEPTABLE_MERGE_STATES = {"DIRTY", "UNKNOWN", "BLOCKED"}
SUCCESS_CHECK_STATES = {"SUCCESS"}
WAITING_CHECK_STATES = {"PENDING", "QUEUED", "EXPECTED", "WAITING", "IN_PROGRESS", "REQUESTED"}


class MergeBlocked(RuntimeError):
    pass


class MergeError(RuntimeError):
    pass


@dataclass
class MergeGateConfig:
    state_dir: Path
    repo: str
    pr_number: int
    output_dir: Path | None = None
    repo_path: Path | None = None
    verify_command: str | None = None
    timeout_seconds: float = 7200
    base_branch: str = "main"
    allow_no_checks: bool = False
    keep_worktree: bool = False


@dataclass
class MergeGateResult:
    decision: dict[str, Any]
    output_dir: Path
    decision_file: Path
    report_file: Path
    summary_file: Path


def run_merge_gate(config: MergeGateConfig) -> MergeGateResult:
    """PR の現状態を評価し、merge 可否の decision artifacts を作る。

    処理フロー:
    - [1] 入力値と local QC の前提を検証する。
    - [2] decision、report、log の出力先を作る。
    - [3] GitHub から PR、checks、base SHA の現状態を取得する。
    - [4] PR 状態と checks から blocker を収集する。
    - [5] 指定されていれば PR head の隔離 worktree で local QC を行う。
    - [6] blocker の優先度から decision payload を確定する。
    - [7] JSON decision、人間向け report、Discord summary を保存する。
    """
    # [1] 評価対象を一意に特定し、local QC を再現できる入力だけを受け付ける。
    if config.pr_number <= 0:
        raise MergeError("--pr must be a positive integer")
    if "/" not in config.repo:
        raise MergeError("--repo must be a GitHub owner/name slug")
    if config.verify_command and config.repo_path is None:
        raise MergeError("--repo-path is required when --verify-command is set")

    # [2] 一回の評価結果をまとめる専用ディレクトリを作り、既存成果物は上書きしない。
    output_dir = (config.output_dir or config.state_dir / "merge-gates" / f"{new_run_id()}-pr{config.pr_number}").expanduser()
    output_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir()

    # [3] decision file 内の自己申告ではなく、GitHub の live state を評価材料にする。
    pr = gh_pr_view(config.repo, config.pr_number)
    checks = gh_pr_checks(config.repo, config.pr_number)
    base_sha = gh_base_sha(config.repo, config.base_branch)
    blockers: list[dict[str, str]] = []

    # [4] open/draft/base/mergeability/checks を個別に評価し、理由をすべて残す。
    if pr.get("state") != "OPEN":
        blockers.append(blocker("MERGE_BLOCKED_NEEDS_HUMAN", f"PR is not open: {pr.get('state')}"))
    if pr.get("isDraft") is True:
        blockers.append(blocker("MERGE_BLOCKED_NEEDS_HUMAN", "draft PR cannot be merged"))
    if pr.get("baseRefName") != config.base_branch:
        blockers.append(blocker("MERGE_BLOCKED_NEEDS_HUMAN", f"unexpected base branch: {pr.get('baseRefName')}"))
    merge_state = str(pr.get("mergeStateStatus") or "")
    if merge_state in UNACCEPTABLE_MERGE_STATES:
        kind = "MERGE_BLOCKED_FIX_REQUIRED" if merge_state == "DIRTY" else "MERGE_BLOCKED_NEEDS_HUMAN"
        blockers.append(blocker(kind, f"PR merge state is not acceptable: {merge_state}"))
    mergeable = str(pr.get("mergeable") or "")
    if mergeable and mergeable != "MERGEABLE":
        kind = "MERGE_BLOCKED_WAITING_CHECKS" if mergeable == "UNKNOWN" else "MERGE_BLOCKED_NEEDS_HUMAN"
        blockers.append(blocker(kind, f"PR is not mergeable: {mergeable}"))

    check_blockers = check_blocking_reasons(checks, config.allow_no_checks or bool(config.verify_command))
    blockers.extend(check_blockers)

    # [5] checks がない構成でも、PR head SHA 固定の local QC で品質条件を評価する。
    local_qc: dict[str, Any] | None = None
    if config.verify_command:
        assert config.repo_path is not None
        local_qc = run_local_qc(
            repo_path=config.repo_path.expanduser().resolve(),
            head_sha=str(pr.get("headRefOid") or ""),
            head_branch=str(pr.get("headRefName") or ""),
            output_dir=output_dir,
            logs_dir=logs_dir,
            verify_command=config.verify_command,
            timeout_seconds=config.timeout_seconds,
            keep_worktree=config.keep_worktree,
        )
        if local_qc["status"] != "succeeded":
            kind = "MERGE_BLOCKED_WAITING_CHECKS" if local_qc.get("timedOut") else "MERGE_BLOCKED_FIX_REQUIRED"
            blockers.append(blocker(kind, f"local QC {local_qc['status']}"))

    # [6] blocker の重要度から単一decisionを選び、判断に使ったlive値も一緒に固定する。
    decision_name = choose_decision(blockers)
    evaluated_at = utc_now()
    decision: dict[str, Any] = {
        "schemaVersion": 1,
        "decision": decision_name,
        "repo": config.repo,
        "prNumber": config.pr_number,
        "prUrl": pr.get("url"),
        "title": pr.get("title"),
        "baseBranch": config.base_branch,
        "baseSha": base_sha,
        "headBranch": pr.get("headRefName"),
        "headSha": pr.get("headRefOid"),
        "mergeStateStatus": pr.get("mergeStateStatus"),
        "mergeable": pr.get("mergeable"),
        "evaluatedAt": evaluated_at,
        "blockingReasons": [item["reason"] for item in blockers],
        "checks": {
            "count": len(checks),
            "nonSuccess": [compact_check(check) for check in checks if str(check.get("state") or "") not in SUCCESS_CHECK_STATES],
        },
        "allowNoChecks": config.allow_no_checks,
    }
    if decision_name == "MERGE_APPROVED":
        decision["approvedAt"] = evaluated_at
    if local_qc is not None:
        decision["localQc"] = local_qc

    # [7] 機械判定、人間の確認、通知が同じdecisionを参照できる3成果物を保存する。
    decision_file = output_dir / "merge-decision.json"
    report_file = output_dir / "merge-gate.md"
    summary_file = output_dir / "hermes-discord-summary.md"
    write_json(decision_file, decision)
    report_file.write_text(render_gate_report(decision, checks), encoding="utf-8")
    summary_file.write_text(render_discord_summary(decision), encoding="utf-8")
    return MergeGateResult(decision, output_dir, decision_file, report_file, summary_file)


def run_merge_approved(
    decision_file: Path,
    execute: bool,
    max_age_seconds: int,
    method: str = "squash",
    delete_branch: bool = True,
    allow_no_checks: bool = False,
    repo_path: Path | None = None,
    verify_command: str | None = None,
    timeout_seconds: float = 7200,
) -> str:
    """承認済み decision を live state で再検証し、dry-run または merge する。

    処理フロー:
    - [1] decision の形式、承認状態、SHA、鮮度を検証する。
    - [2] PR と base の live state が decision 作成時から変わっていないか再検証する。
    - [3] live checks、または同じ head SHA の local QC を再検証する。
    - [4] dry-run では merge 対象だけを返して終了する。
    - [5] execute 時は head SHA lock 付きで GitHub merge を実行する。
    - [6] GitHub merge の失敗を block として返し、成功結果だけを返す。
    """
    # [1] 編集可能なJSONをそのまま信用せず、必要fieldと承認時刻を先に検証する。
    decision = json.loads(decision_file.expanduser().read_text(encoding="utf-8"))
    repo, pr_number, base_branch, base_sha, head_sha = validate_decision_shape(decision, max_age_seconds)

    # [2] head/base SHA、open/draft、merge state、mergeable をGitHubから取り直して比較する。
    pr = gh_pr_view(repo, pr_number)
    actual_head_sha = str(pr.get("headRefOid") or "")
    actual_base = str(pr.get("baseRefName") or "")
    if actual_head_sha != head_sha:
        raise MergeBlocked(f"head SHA changed: decision={head_sha} actual={actual_head_sha}")
    if actual_base != base_branch:
        raise MergeBlocked(f"PR base changed: decision={base_branch} actual={actual_base}")
    current_base_sha = gh_base_sha(repo, base_branch)
    if current_base_sha != base_sha:
        raise MergeBlocked(f"base branch advanced: decision={base_sha} actual={current_base_sha}")
    if pr.get("state") != "OPEN":
        raise MergeBlocked(f"PR is not open: {pr.get('state')}")
    if pr.get("isDraft") is True:
        raise MergeBlocked("draft PR cannot be merged")
    merge_state = str(pr.get("mergeStateStatus") or "")
    if merge_state in UNACCEPTABLE_MERGE_STATES:
        raise MergeBlocked(f"PR merge state is not acceptable: {merge_state}")
    mergeable = str(pr.get("mergeable") or "")
    if mergeable != "MERGEABLE":
        raise MergeBlocked(f"PR is not mergeable: {mergeable or 'unknown'}")

    # [3] live checks を優先し、checks がない場合は同じ head SHA で local QC をやり直す。
    checks = gh_pr_checks(repo, pr_number)
    if checks:
        check_blockers = check_blocking_reasons(checks, allow_no_checks=False)
        if check_blockers:
            raise MergeBlocked("; ".join(item["reason"] for item in check_blockers))
    elif not allow_no_checks:
        recheck_local_qc(
            repo_path=repo_path,
            head_sha=head_sha,
            head_branch=str(pr.get("headRefName") or ""),
            verify_command=verify_command,
            timeout_seconds=timeout_seconds,
        )

    # [4] default は副作用のないdry-runとし、再検証済みの対象だけを表示する。
    url = str(pr.get("url") or f"https://github.com/{repo}/pull/{pr_number}")
    if not execute:
        return f"dry-run: would merge {url} at {head_sha}"

    # [5] decision と同じ head 以外をmergeしないよう、GitHub側にもSHA lockを渡す。
    args = ["gh", "pr", "merge", str(pr_number), "--repo", repo, f"--{method}", "--match-head-commit", head_sha]
    if delete_branch:
        args.insert(-2, "--delete-branch")
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # [6] GitHub CLI の失敗を成功として扱わず、上位CLIが判別できる block に変換する。
    if result.returncode != 0:
        raise MergeBlocked(result.stdout.strip() or "gh pr merge failed")
    return result.stdout.strip() or f"merged {url} at {head_sha}"


def validate_decision_shape(decision: dict[str, Any], max_age_seconds: int) -> tuple[str, int, str, str, str]:
    if decision.get("schemaVersion") != 1:
        raise MergeBlocked(f"unsupported or missing schemaVersion: {decision.get('schemaVersion')}")
    if decision.get("decision") != "MERGE_APPROVED":
        raise MergeBlocked(f"decision is not MERGE_APPROVED: {decision.get('decision')}")
    repo = str(decision.get("repo") or "")
    if "/" not in repo:
        raise MergeBlocked(f"invalid repo: {repo}")
    pr_number = decision.get("prNumber")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise MergeBlocked(f"invalid prNumber: {pr_number}")
    base_branch = str(decision.get("baseBranch") or "")
    if not base_branch:
        raise MergeBlocked("missing baseBranch")
    base_sha = str(decision.get("baseSha") or "")
    head_sha = str(decision.get("headSha") or "")
    if not SHA_RE.match(base_sha):
        raise MergeBlocked(f"invalid baseSha: {base_sha}")
    if not SHA_RE.match(head_sha):
        raise MergeBlocked(f"invalid headSha: {head_sha}")
    approved_at = parse_approved_at(str(decision.get("approvedAt") or ""))
    age_seconds = int((datetime.now(timezone.utc) - approved_at).total_seconds())
    if age_seconds < 0:
        raise MergeBlocked(f"approvedAt is in the future: {decision.get('approvedAt')}")
    if age_seconds > max_age_seconds:
        raise MergeBlocked(f"merge decision is stale: age={age_seconds}s max={max_age_seconds}s")
    return repo, pr_number, base_branch, base_sha, head_sha


def parse_approved_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MergeBlocked(f"invalid approvedAt: {value}") from exc
    if parsed.tzinfo is None:
        raise MergeBlocked(f"invalid approvedAt: {value}")
    return parsed.astimezone(timezone.utc)


def recheck_local_qc(
    repo_path: Path | None,
    head_sha: str,
    head_branch: str,
    verify_command: str | None,
    timeout_seconds: float,
) -> None:
    if repo_path is None or not verify_command:
        raise MergeBlocked(
            "no PR checks reported; pass --repo-path and --verify-command to re-run local QC, "
            "or explicitly pass --allow-no-checks"
        )
    with tempfile.TemporaryDirectory(prefix="agent-workflow-merge-qc-") as temp_dir:
        output_dir = Path(temp_dir)
        logs_dir = output_dir / "logs"
        logs_dir.mkdir()
        result = run_local_qc(
            repo_path=repo_path.expanduser().resolve(),
            head_sha=head_sha,
            head_branch=head_branch,
            output_dir=output_dir,
            logs_dir=logs_dir,
            verify_command=verify_command,
            timeout_seconds=timeout_seconds,
            keep_worktree=False,
        )
    if result["status"] != "succeeded":
        error = result.get("error") or f"exit={result.get('exitCode')}"
        raise MergeBlocked(f"local QC re-check {result['status']}: {error}")


def gh_pr_view(repo: str, pr_number: int) -> dict[str, Any]:
    fields = "number,state,isDraft,baseRefName,headRefName,headRefOid,mergeStateStatus,mergeable,url,title"
    return gh_json(["pr", "view", str(pr_number), "--repo", repo, "--json", fields])


def gh_base_sha(repo: str, base_branch: str) -> str:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/git/ref/heads/{base_branch}", "--jq", ".object.sha"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        raise MergeError(result.stdout.strip() or "gh api failed")
    sha = result.stdout.strip()
    if not SHA_RE.match(sha):
        raise MergeError(f"GitHub returned invalid base SHA: {sha}")
    return sha


def gh_pr_checks(repo: str, pr_number: int) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["gh", "pr", "checks", str(pr_number), "--repo", repo, "--json", "name,state,bucket,link"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        output = result.stdout.strip()
        if "no checks reported" in output or "unknown flag: --json" in output:
            return []
        raise MergeError(output or "gh pr checks failed")
    if not result.stdout.strip():
        return []
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise MergeError("gh pr checks returned non-list JSON")
    return data


def gh_json(args: list[str]) -> dict[str, Any]:
    result = subprocess.run(["gh", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise MergeError(result.stdout.strip() or f"gh {' '.join(args)} failed")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise MergeError(f"gh {' '.join(args)} returned non-object JSON")
    return data


def check_blocking_reasons(checks: list[dict[str, Any]], allow_no_checks: bool) -> list[dict[str, str]]:
    if not checks:
        if allow_no_checks:
            return []
        return [blocker("MERGE_BLOCKED_NEEDS_HUMAN", "no PR checks reported and no local QC approval is available")]

    blockers: list[dict[str, str]] = []
    for check in checks:
        state = str(check.get("state") or "")
        if state in SUCCESS_CHECK_STATES:
            continue
        name = str(check.get("name") or "unknown")
        bucket = str(check.get("bucket") or "")
        if state in WAITING_CHECK_STATES or bucket == "pending":
            blockers.append(blocker("MERGE_BLOCKED_WAITING_CHECKS", f"check is still running: {name}:{state}:{bucket}"))
        else:
            blockers.append(blocker("MERGE_BLOCKED_FIX_REQUIRED", f"check is not successful: {name}:{state}:{bucket}"))
    return blockers


def run_local_qc(
    repo_path: Path,
    head_sha: str,
    head_branch: str,
    output_dir: Path,
    logs_dir: Path,
    verify_command: str,
    timeout_seconds: float,
    keep_worktree: bool,
) -> dict[str, Any]:
    """指定された head SHA の隔離 worktree で検証commandを実行する。

    処理フロー:
    - [1] 失敗を既定値とする結果と worktree path を用意する。
    - [2] head commit を取得し、その SHA の detached worktree を作る。
    - [3] timeout と log 保存付きで検証 command を実行する。
    - [4] 成功、失敗、timeout、例外を結果へ記録する。
    - [5] 指定がなければ worktree を必ず削除する。
    """
    # [1] 途中で例外が起きても成功扱いにならない初期状態を作る。
    worktree_root = output_dir / "worktree"
    worktree = worktree_root / "repo"
    local_qc: dict[str, Any] = {
        "status": "failed",
        "command": verify_command,
        "timedOut": False,
        "worktree": str(worktree),
    }
    try:
        # [2] PR headを手元へ用意し、現在のbranchを汚さないdetached worktreeを作る。
        ensure_commit_available(repo_path, head_sha, head_branch)
        run_git(repo_path, ["worktree", "add", "--detach", str(worktree), head_sha])
        # [3] 検証の標準出力・標準エラーを成果物へ残し、上限時間も適用する。
        result = run_logged(["bash", "-lc", verify_command], worktree, logs_dir, "local_qc", timeout_seconds)
        # [4] exit code と timeout の両方を使って最終statusを確定する。
        local_qc.update(
            {
                "status": "succeeded" if result.exit_code == 0 and not result.timed_out else ("timed_out" if result.timed_out else "failed"),
                "exitCode": result.exit_code,
                "timedOut": result.timed_out,
                "stdoutPath": result.stdout_path,
                "stderrPath": result.stderr_path,
            }
        )
    except Exception as exc:
        # [4] commit取得やworktree作成を含む例外も、decisionに残せる形へ変換する。
        local_qc["error"] = str(exc)
    finally:
        # [5] 調査目的で保持する指定がない限り、一時worktreeを必ず片付ける。
        if not keep_worktree:
            subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            shutil.rmtree(worktree_root, ignore_errors=True)
        else:
            local_qc["kept"] = True
    return local_qc


def ensure_commit_available(repo_path: Path, head_sha: str, head_branch: str) -> None:
    if not SHA_RE.match(head_sha):
        raise MergeError(f"invalid head SHA: {head_sha}")
    result = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", f"{head_sha}^{{commit}}"])
    if result.returncode == 0:
        return
    if head_branch:
        subprocess.run(["git", "-C", str(repo_path), "fetch", "origin", head_branch], check=False)
    result = subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", f"{head_sha}^{{commit}}"])
    if result.returncode != 0:
        raise MergeError(f"head commit is not available locally: {head_sha}")


def run_git(repo_path: Path, args: list[str]) -> None:
    result = subprocess.run(["git", "-C", str(repo_path), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise MergeError(result.stdout.strip() or f"git {' '.join(args)} failed")


def choose_decision(blockers: list[dict[str, str]]) -> str:
    if not blockers:
        return "MERGE_APPROVED"
    kinds = {item["kind"] for item in blockers}
    if "MERGE_BLOCKED_FIX_REQUIRED" in kinds:
        return "MERGE_BLOCKED_FIX_REQUIRED"
    if "MERGE_BLOCKED_WAITING_CHECKS" in kinds:
        return "MERGE_BLOCKED_WAITING_CHECKS"
    return "MERGE_BLOCKED_NEEDS_HUMAN"


def blocker(kind: str, reason: str) -> dict[str, str]:
    return {"kind": kind, "reason": reason}


def compact_check(check: dict[str, Any]) -> dict[str, Any]:
    return {key: check.get(key) for key in ["name", "state", "bucket", "link"] if check.get(key) is not None}


def render_gate_report(decision: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    lines = [
        f"# merge gate PR #{decision['prNumber']}",
        "",
        f"- decision: `{decision['decision']}`",
        f"- repo: `{decision['repo']}`",
        f"- base: `{decision['baseBranch']}` `{decision['baseSha']}`",
        f"- head: `{decision.get('headBranch')}` `{decision.get('headSha')}`",
        f"- pr: `{decision.get('prUrl')}`",
        "",
        "## blockers",
    ]
    blockers = decision.get("blockingReasons") or []
    if blockers:
        lines.extend(f"- {reason}" for reason in blockers)
    else:
        lines.append("- none")

    lines.extend(["", "## checks"])
    if checks:
        for check in checks:
            lines.append(f"- {check.get('name')}: `{check.get('state')}` bucket=`{check.get('bucket')}`")
    else:
        lines.append("- no GitHub checks reported")

    local_qc = decision.get("localQc")
    if isinstance(local_qc, dict):
        lines.extend(
            [
                "",
                "## local QC",
                f"- status: `{local_qc.get('status')}`",
                f"- command: `{local_qc.get('command')}`",
                f"- exit: `{local_qc.get('exitCode', '')}`",
                f"- timed_out: `{local_qc.get('timedOut')}`",
                f"- stdout: `{local_qc.get('stdoutPath', '')}`",
                f"- stderr: `{local_qc.get('stderrPath', '')}`",
            ]
        )
        if local_qc.get("error"):
            lines.append(f"- error: `{local_qc.get('error')}`")
    lines.append("")
    return "\n".join(lines)


def render_discord_summary(decision: dict[str, Any]) -> str:
    status = str(decision["decision"])
    title = str(decision.get("title") or "")
    url = str(decision.get("prUrl") or "")
    if status == "MERGE_APPROVED":
        headline = f"✅ merge approved: PR #{decision['prNumber']}"
    else:
        headline = f"🛑 merge blocked: PR #{decision['prNumber']} ({status})"
    lines = [headline]
    if title:
        lines.append(f"📝 {title}")
    if url:
        lines.append(f"🔗 {url}")
    lines.append("")

    blocking_reasons = decision.get("blockingReasons") or []
    if blocking_reasons:
        lines.append("🔎 needs attention")
        for reason in blocking_reasons:
            lines.append(f"- {reason}")
    else:
        lines.append("🎉 all configured merge gates passed")
        lines.append("🚀 ready for a safe merge")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
