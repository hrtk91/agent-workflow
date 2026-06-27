from __future__ import annotations

import configparser
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent_workflow.runner import FAILURE_NOTIFY_STATUSES, new_run_id, utc_now

REPAIR_CATEGORIES = {
    "dependency_missing",
    "deploy_config",
    "deploy_runtime",
    "implementation_failure",
    "repo_config",
    "runtime_env",
    "test_infra_flake",
    "timeout",
    "transient_external",
    "unknown",
}
REPAIR_RISKS = {"low", "medium", "high"}
REPAIR_ACTIONS = {
    "dependency_install_or_update",
    "gateway_restart",
    "human_needed",
    "migration_with_healthcheck",
    "redeploy_and_healthcheck",
    "repo_config_patch",
    "resume_original_run",
    "retry_original_run",
    "runtime_environment_patch",
    "worktree_cleanup_and_retry",
}
VERIFY_REQUIRED_ACTIONS = {
    "dependency_install_or_update",
    "gateway_restart",
    "repo_config_patch",
    "runtime_environment_patch",
    "worktree_cleanup_and_retry",
}
DEPLOYMENT_ACTIONS = {"migration_with_healthcheck", "redeploy_and_healthcheck"}


@dataclass(frozen=True)
class RepairDraftInput:
    failed_run_id: str
    title: str
    category: str
    risk: str
    proposed_action: str
    diagnosis_file: Path
    evidence_file: Path
    notify_before_file: Path
    verify_command: str | None = None
    retry_original: bool = False
    environment: str | None = None
    healthcheck_command: str | None = None
    rollback_plan_file: Path | None = None


@dataclass(frozen=True)
class RepairDraft:
    draft_id: str
    draft_dir: Path
    status: str


class RepairManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.runs_dir = self.state_dir / "runs"
        self.repairs_dir = self.state_dir / "repairs"
        self.db_path = self.state_dir / "jobs.sqlite"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.repairs_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def draft(self, draft_input: RepairDraftInput, allow_duplicate: bool = False) -> RepairDraft:
        self._validate_input(draft_input)
        if not allow_duplicate:
            existing = self._existing_draft(draft_input.failed_run_id, draft_input.proposed_action)
            if existing:
                raise ValueError(
                    "repair draft already exists for "
                    f"{draft_input.failed_run_id}/{draft_input.proposed_action}: {existing}"
                )

        draft_id = new_run_id()
        draft_dir = self.repairs_dir / draft_id
        draft_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(draft_input.diagnosis_file, draft_dir / "diagnosis.md")
        shutil.copy2(draft_input.evidence_file, draft_dir / "evidence.md")
        shutil.copy2(draft_input.notify_before_file, draft_dir / "notify-before.md")
        if draft_input.rollback_plan_file:
            shutil.copy2(draft_input.rollback_plan_file, draft_dir / "rollback-plan.md")

        now = utc_now()
        config = configparser.ConfigParser()
        config["repair"] = {
            "draft_id": draft_id,
            "failed_run_id": draft_input.failed_run_id,
            "status": "drafted",
            "title": draft_input.title.strip(),
            "category": draft_input.category,
            "risk": draft_input.risk,
            "proposed_action": draft_input.proposed_action,
            "retry_original": str(bool(draft_input.retry_original)).lower(),
            "created_at": now,
            "updated_at": now,
            "failed_run_state": str(self._state_path(draft_input.failed_run_id)),
            "diagnosis": "diagnosis.md",
            "evidence": "evidence.md",
            "notify_before": "notify-before.md",
            "verify_command": draft_input.verify_command or "",
            "environment": draft_input.environment or "",
            "healthcheck_command": draft_input.healthcheck_command or "",
            "rollback_plan": "rollback-plan.md" if draft_input.rollback_plan_file else "",
        }
        self._write_config(draft_dir, config)
        self._upsert_draft_row(draft_id, draft_input.failed_run_id, draft_input.category, draft_input.risk, draft_input.proposed_action, "drafted", draft_dir)
        return self.validate(draft_id=draft_id)

    def validate(self, draft_id: str | None = None, draft_dir: Path | None = None) -> RepairDraft:
        if draft_id:
            draft_dir = self.repairs_dir / draft_id
        if draft_dir is None:
            raise ValueError("one of draft_id or draft_dir is required")
        draft_dir = draft_dir.expanduser().resolve()
        config_path = draft_dir / "repair.ini"
        if not config_path.exists():
            raise ValueError(f"missing repair.ini: {config_path}")
        config = configparser.ConfigParser()
        config.read(config_path, encoding="utf-8")
        if "repair" not in config:
            raise ValueError("repair.ini must contain [repair]")
        data = config["repair"]
        draft_id_value = self._required(data, "draft_id")
        failed_run_id = self._required(data, "failed_run_id")
        category = self._required(data, "category")
        risk = self._required(data, "risk")
        proposed_action = self._required(data, "proposed_action")
        title = self._required(data, "title")
        if len(title) > 200:
            raise ValueError("title must be 200 characters or shorter")
        self._validate_choices(category, risk, proposed_action)
        self._validate_failed_run(failed_run_id)

        self._require_non_empty(draft_dir / self._required(data, "diagnosis"))
        self._require_non_empty(draft_dir / self._required(data, "evidence"))
        self._require_non_empty(draft_dir / self._required(data, "notify_before"))

        verify_command = data.get("verify_command", "").strip()
        if proposed_action in VERIFY_REQUIRED_ACTIONS and not verify_command:
            raise ValueError(f"{proposed_action} requires verify_command")

        if proposed_action in DEPLOYMENT_ACTIONS:
            if risk != "high":
                raise ValueError(f"{proposed_action} must use risk=high")
            if not data.get("environment", "").strip():
                raise ValueError(f"{proposed_action} requires environment")
            if not data.get("healthcheck_command", "").strip():
                raise ValueError(f"{proposed_action} requires healthcheck_command")
            rollback_plan = data.get("rollback_plan", "").strip()
            if not rollback_plan:
                raise ValueError(f"{proposed_action} requires rollback_plan")
            self._require_non_empty(draft_dir / rollback_plan)

        now = utc_now()
        data["status"] = "validated"
        data["updated_at"] = now
        self._write_config(draft_dir, config)
        self._upsert_draft_row(draft_id_value, failed_run_id, category, risk, proposed_action, "validated", draft_dir)
        return RepairDraft(draft_id=draft_id_value, draft_dir=draft_dir, status="validated")

    def scan_failures(self, limit: int = 20, include_repaired: bool = False) -> list[dict[str, str]]:
        params: list[object] = [*sorted(FAILURE_NOTIFY_STATUSES)]
        repair_filter = ""
        if not include_repaired:
            repair_filter = "and repairs.repair_status is null"
        params.append(limit)
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                select
                  jobs.run_id,
                  jobs.status,
                  coalesce(jobs.current_step, '') as current_step,
                  jobs.summary_path,
                  coalesce(repairs.repair_status, '') as repair_status
                from jobs
                left join (
                  select failed_run_id, group_concat(draft_id || ':' || status, ',') as repair_status
                  from repair_drafts
                  group by failed_run_id
                ) repairs on repairs.failed_run_id = jobs.run_id
                where jobs.status in ({','.join('?' for _ in FAILURE_NOTIFY_STATUSES)})
                {repair_filter}
                order by jobs.created_at desc
                limit ?
                """,
                params,
            ).fetchall()
        return [
            {
                "run_id": str(row["run_id"]),
                "status": str(row["status"]),
                "current_step": str(row["current_step"]),
                "summary_path": str(row["summary_path"]),
                "repair_status": str(row["repair_status"]),
            }
            for row in rows
        ]

    def _validate_input(self, draft_input: RepairDraftInput) -> None:
        if not draft_input.title.strip():
            raise ValueError("--title is required")
        if len(draft_input.title.strip()) > 200:
            raise ValueError("--title must be 200 characters or shorter")
        self._validate_choices(draft_input.category, draft_input.risk, draft_input.proposed_action)
        self._validate_failed_run(draft_input.failed_run_id)
        self._require_non_empty(draft_input.diagnosis_file)
        self._require_non_empty(draft_input.evidence_file)
        self._require_non_empty(draft_input.notify_before_file)
        if draft_input.proposed_action in VERIFY_REQUIRED_ACTIONS and not (draft_input.verify_command or "").strip():
            raise ValueError(f"{draft_input.proposed_action} requires --verify-command")
        if draft_input.proposed_action in DEPLOYMENT_ACTIONS:
            if draft_input.risk != "high":
                raise ValueError(f"{draft_input.proposed_action} requires --risk high")
            if not (draft_input.environment or "").strip():
                raise ValueError(f"{draft_input.proposed_action} requires --environment")
            if not (draft_input.healthcheck_command or "").strip():
                raise ValueError(f"{draft_input.proposed_action} requires --healthcheck-command")
            if draft_input.rollback_plan_file is None:
                raise ValueError(f"{draft_input.proposed_action} requires --rollback-plan-file")
            self._require_non_empty(draft_input.rollback_plan_file)

    def _validate_choices(self, category: str, risk: str, proposed_action: str) -> None:
        if category not in REPAIR_CATEGORIES:
            raise ValueError(f"unknown category {category}; expected one of {', '.join(sorted(REPAIR_CATEGORIES))}")
        if risk not in REPAIR_RISKS:
            raise ValueError(f"unknown risk {risk}; expected one of {', '.join(sorted(REPAIR_RISKS))}")
        if proposed_action not in REPAIR_ACTIONS:
            raise ValueError(f"unknown proposed_action {proposed_action}; expected one of {', '.join(sorted(REPAIR_ACTIONS))}")

    def _validate_failed_run(self, run_id: str) -> None:
        state_path = self._state_path(run_id)
        if not state_path.exists():
            raise ValueError(f"failed run state does not exist: {state_path}")
        data = json.loads(state_path.read_text(encoding="utf-8"))
        status = str(data.get("status") or "")
        if status not in FAILURE_NOTIFY_STATUSES:
            raise ValueError(f"run {run_id} is {status}, expected one of {', '.join(sorted(FAILURE_NOTIFY_STATUSES))}")

    def _existing_draft(self, failed_run_id: str, proposed_action: str) -> str:
        with self._db() as conn:
            row = conn.execute(
                """
                select draft_id
                from repair_drafts
                where failed_run_id = ? and proposed_action = ?
                order by created_at desc
                limit 1
                """,
                (failed_run_id, proposed_action),
            ).fetchone()
        return str(row[0]) if row else ""

    def _upsert_draft_row(
        self,
        draft_id: str,
        failed_run_id: str,
        category: str,
        risk: str,
        proposed_action: str,
        status: str,
        draft_dir: Path,
    ) -> None:
        now = utc_now()
        with self._db() as conn:
            conn.execute(
                """
                insert into repair_drafts(
                  draft_id, failed_run_id, category, risk, proposed_action, status, draft_dir, created_at, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(draft_id) do update set
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (draft_id, failed_run_id, category, risk, proposed_action, status, str(draft_dir), now, now),
            )

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute(
                """
                create table if not exists repair_drafts (
                  draft_id text primary key,
                  failed_run_id text not null,
                  category text not null,
                  risk text not null,
                  proposed_action text not null,
                  status text not null,
                  draft_dir text not null,
                  created_at text not null,
                  updated_at text not null
                )
                """
            )

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode=wal")
        return conn

    def _state_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "state.json"

    def _write_config(self, draft_dir: Path, config: configparser.ConfigParser) -> None:
        with (draft_dir / "repair.ini").open("w", encoding="utf-8") as f:
            config.write(f)

    def _required(self, data: configparser.SectionProxy, key: str) -> str:
        value = data.get(key, "").strip()
        if not value:
            raise ValueError(f"repair.ini missing {key}")
        return value

    def _require_non_empty(self, path: Path) -> None:
        path = path.expanduser()
        if not path.exists():
            raise ValueError(f"required file does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"required path is not a file: {path}")
        if path.stat().st_size == 0:
            raise ValueError(f"required file is empty: {path}")
