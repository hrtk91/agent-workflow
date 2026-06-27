from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AW = ROOT / "scripts" / "aw"


class MergeCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.repo, self.base_sha, self.head_sha = self._make_repo()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.gh_state = self.root / "gh-state.json"
        self.merge_call = self.root / "merge-call.json"
        self._write_fake_gh()
        self._write_gh_state(checks=[])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_gate_approves_with_successful_local_qc(self) -> None:
        output_dir = self.root / "gate-approved"
        result = self._aw(
            "merge-gate",
            "--repo",
            "hrtk91/eb-temp",
            "--pr",
            "853",
            "--repo-path",
            str(self.repo),
            "--verify-command",
            "test -f feature.txt",
            "--output-dir",
            str(output_dir),
        )

        decision_path = Path(result.stdout.strip())
        decision = json.loads(decision_path.read_text())
        self.assertEqual("MERGE_APPROVED", decision["decision"])
        self.assertEqual(self.head_sha, decision["headSha"])
        self.assertEqual("succeeded", decision["localQc"]["status"])
        self.assertFalse((output_dir / "worktree").exists())
        self.assertTrue((output_dir / "merge-gate.md").exists())
        self.assertTrue((output_dir / "hermes-discord-summary.md").exists())
        summary = (output_dir / "hermes-discord-summary.md").read_text(encoding="utf-8")
        self.assertIn("✅ merge approved: PR #853", summary)
        self.assertIn("📝 fixture PR", summary)
        self.assertIn("🎉 all configured merge gates passed", summary)
        self.assertIn("🚀 ready for a safe merge", summary)

    def test_merge_gate_blocks_when_no_checks_and_no_local_qc(self) -> None:
        output_dir = self.root / "gate-blocked"
        result = self._aw(
            "merge-gate",
            "--repo",
            "hrtk91/eb-temp",
            "--pr",
            "853",
            "--output-dir",
            str(output_dir),
            check=False,
        )

        self.assertEqual(1, result.returncode)
        decision = json.loads((output_dir / "merge-decision.json").read_text())
        self.assertEqual("MERGE_BLOCKED_NEEDS_HUMAN", decision["decision"])
        self.assertIn("no PR checks reported", "\n".join(decision["blockingReasons"]))
        summary = (output_dir / "hermes-discord-summary.md").read_text(encoding="utf-8")
        self.assertIn("🛑 merge blocked: PR #853 (MERGE_BLOCKED_NEEDS_HUMAN)", summary)
        self.assertIn("🔎 needs attention", summary)
        self.assertIn("- no PR checks reported", summary)

    def test_merge_dry_run_and_execute_use_head_lock(self) -> None:
        decision_path = self.root / "approved.json"
        decision_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "decision": "MERGE_APPROVED",
                    "repo": "hrtk91/eb-temp",
                    "prNumber": 853,
                    "baseBranch": "main",
                    "baseSha": self.base_sha,
                    "headBranch": "feature",
                    "headSha": self.head_sha,
                    "approvedAt": datetime.now(timezone.utc).isoformat(),
                    "localQc": {"status": "succeeded"},
                }
            )
            + "\n"
        )

        dry_run = self._aw("merge", "--decision", str(decision_path))
        self.assertIn(f"dry-run: would merge https://github.com/hrtk91/eb-temp/pull/853 at {self.head_sha}", dry_run.stdout)

        executed = self._aw("merge", "--decision", str(decision_path), "--execute")
        self.assertIn("merged", executed.stdout)
        merge_args = json.loads(self.merge_call.read_text())
        self.assertIn("--squash", merge_args)
        self.assertIn("--delete-branch", merge_args)
        self.assertEqual(self.head_sha, merge_args[merge_args.index("--match-head-commit") + 1])

    def test_merge_rejects_approved_decision_without_checks_or_local_qc(self) -> None:
        decision_path = self.root / "unsafe-approved.json"
        decision_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "decision": "MERGE_APPROVED",
                    "repo": "hrtk91/eb-temp",
                    "prNumber": 853,
                    "baseBranch": "main",
                    "baseSha": self.base_sha,
                    "headBranch": "feature",
                    "headSha": self.head_sha,
                    "approvedAt": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )

        result = self._aw("merge", "--decision", str(decision_path), check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("no PR checks reported", result.stderr)

    def test_merge_gate_allows_old_gh_checks_without_json_when_local_qc_passes(self) -> None:
        self._write_gh_state(checks=None)
        output_dir = self.root / "gate-old-gh"
        result = self._aw(
            "merge-gate",
            "--repo",
            "hrtk91/eb-temp",
            "--pr",
            "853",
            "--repo-path",
            str(self.repo),
            "--verify-command",
            "test -f feature.txt",
            "--output-dir",
            str(output_dir),
        )

        decision = json.loads(Path(result.stdout.strip()).read_text())
        self.assertEqual("MERGE_APPROVED", decision["decision"])
        self.assertEqual(0, decision["checks"]["count"])

    def _aw(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        env["FAKE_GH_STATE"] = str(self.gh_state)
        env["FAKE_GH_MERGE_CALL"] = str(self.merge_call)
        result = subprocess.run(
            [str(AW), "--state-dir", str(self.state_dir), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        if check and result.returncode != 0:
            self.fail(f"aw failed with {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def _make_repo(self) -> tuple[Path, str, str]:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "agent-workflow-test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "agent-workflow-test@example.invalid"], cwd=repo, check=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE)
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        subprocess.run(["git", "switch", "-c", "feature"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True, stdout=subprocess.PIPE)
        head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        subprocess.run(["git", "switch", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return repo, base_sha, head_sha

    def _write_gh_state(self, checks: list[dict[str, object]]) -> None:
        self.gh_state.write_text(
            json.dumps(
                {
                    "baseSha": self.base_sha,
                    "checks": checks,
                    "pr": {
                        "number": 853,
                        "state": "OPEN",
                        "isDraft": False,
                        "baseRefName": "main",
                        "headRefName": "feature",
                        "headRefOid": self.head_sha,
                        "mergeStateStatus": "CLEAN",
                        "mergeable": "MERGEABLE",
                        "url": "https://github.com/hrtk91/eb-temp/pull/853",
                        "title": "fixture PR",
                    },
                }
            )
            + "\n"
        )

    def _write_fake_gh(self) -> None:
        path = self.fake_bin / "gh"
        path.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text())

if args[:2] == ["pr", "view"]:
    print(json.dumps(state["pr"]))
elif args[:2] == ["pr", "checks"]:
    checks = state.get("checks")
    if checks is None:
        print("unknown flag: --json")
        sys.exit(1)
    print(json.dumps(checks))
elif args and args[0] == "api":
    print(state["baseSha"])
elif args[:2] == ["pr", "merge"]:
    Path(os.environ["FAKE_GH_MERGE_CALL"]).write_text(json.dumps(args))
    print("merged")
else:
    print("unexpected gh args: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
