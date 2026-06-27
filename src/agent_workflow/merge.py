from __future__ import annotations

import json
import re
import shutil
import subprocess
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
    if config.pr_number <= 0:
        raise MergeError("--pr must be a positive integer")
    if "/" not in config.repo:
        raise MergeError("--repo must be a GitHub owner/name slug")
    if config.verify_command and config.repo_path is None:
        raise MergeError("--repo-path is required when --verify-command is set")

    output_dir = (config.output_dir or config.state_dir / "merge-gates" / f"{new_run_id()}-pr{config.pr_number}").expanduser()
    output_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir()

    pr = gh_pr_view(config.repo, config.pr_number)
    checks = gh_pr_checks(config.repo, config.pr_number)
    base_sha = gh_base_sha(config.repo, config.base_branch)
    blockers: list[dict[str, str]] = []

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
) -> str:
    decision = json.loads(decision_file.expanduser().read_text(encoding="utf-8"))
    repo, pr_number, base_branch, base_sha, head_sha = validate_decision_shape(decision, max_age_seconds)

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
    if pr.get("isDraft") is True:
        raise MergeBlocked("draft PR cannot be merged")
    merge_state = str(pr.get("mergeStateStatus") or "")
    if merge_state in UNACCEPTABLE_MERGE_STATES:
        raise MergeBlocked(f"PR merge state is not acceptable: {merge_state}")

    checks = gh_pr_checks(repo, pr_number)
    check_blockers = check_blocking_reasons(checks, allow_no_checks or decision_allows_no_checks(decision))
    if check_blockers:
        raise MergeBlocked("; ".join(item["reason"] for item in check_blockers))

    url = str(pr.get("url") or f"https://github.com/{repo}/pull/{pr_number}")
    if not execute:
        return f"dry-run: would merge {url} at {head_sha}"

    args = ["gh", "pr", "merge", str(pr_number), "--repo", repo, f"--{method}", "--match-head-commit", head_sha]
    if delete_branch:
        args.insert(-2, "--delete-branch")
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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


def decision_allows_no_checks(decision: dict[str, Any]) -> bool:
    local_qc = decision.get("localQc")
    if isinstance(local_qc, dict) and local_qc.get("status") == "succeeded":
        return True
    return bool(decision.get("allowNoChecks"))


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
    worktree_root = output_dir / "worktree"
    worktree = worktree_root / "repo"
    local_qc: dict[str, Any] = {
        "status": "failed",
        "command": verify_command,
        "timedOut": False,
        "worktree": str(worktree),
    }
    try:
        ensure_commit_available(repo_path, head_sha, head_branch)
        run_git(repo_path, ["worktree", "add", "--detach", str(worktree), head_sha])
        result = run_logged(["bash", "-lc", verify_command], worktree, logs_dir, "local_qc", timeout_seconds)
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
        local_qc["error"] = str(exc)
    finally:
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
        headline = f"merge approved: PR #{decision['prNumber']}"
    else:
        headline = f"merge blocked: PR #{decision['prNumber']} ({status})"
    lines = [headline, title, url, ""]
    for reason in decision.get("blockingReasons") or []:
        lines.append(f"- {reason}")
    if not decision.get("blockingReasons"):
        lines.append("- all configured merge gates passed")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
