from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CandidateChain = list[tuple[str | None, str | None]]


def _coalesce_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_candidate_chain(raw: object) -> CandidateChain:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    chain: CandidateChain = []
    for item in raw:
        if isinstance(item, dict):
            chain.append((_coalesce_optional(item.get("provider")), _coalesce_optional(item.get("model"))))
            continue
        if isinstance(item, (list, tuple)):
            if len(item) >= 2:
                chain.append((_coalesce_optional(item[0]), _coalesce_optional(item[1])))
            continue
        if isinstance(item, str):
            if "::" in item:
                provider, model = item.split("::", 1)
            elif ":" in item:
                provider, model = item.split(":", 1)
            else:
                provider, model = item, None
            chain.append((_coalesce_optional(provider), _coalesce_optional(model)))
            continue
    return chain

WORKFLOW_STEPS = ("load_task", "create_worktree", "run_executor", "run_qc", "write_summary")


@dataclass
class StepState:
    name: str
    status: str = "pending"
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    error: str | None = None
    candidate_index: int | None = None
    candidate_provider: str | None = None
    candidate_model: str | None = None
    candidate_execution_id: str | None = None
    failure_category: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepState":
        return cls(**data)


@dataclass
class RunState:
    run_id: str
    status: str
    repo_path: str
    run_dir: str
    task_dir: str
    workflow: str
    verify_command: str
    timeout_seconds: float
    executor_bin: str
    provider: str | None = None
    model: str | None = None
    candidate_chain: CandidateChain = field(default_factory=list)
    candidate_index: int = 0
    candidate_checkpoint: str = ""
    lineage_id: str = ""
    task_type: str = "unspecified"
    base_ref: str | None = None
    purpose: str = "workflow"
    repair_for_run_id: str | None = None
    worktree_path: str | None = None
    summary_path: str = ""
    current_step: str | None = None
    # Number of QC→executor repair loops already started for this run.
    # Persisted so resume/retry share the same run-level budget (max QC_REPAIR_MAX_ATTEMPTS).
    qc_repair_attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    steps: list[StepState] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        data = dict(data)
        if "takt_bin" in data and "executor_bin" not in data:
            data["executor_bin"] = data.pop("takt_bin")
        # trace.jsonlを持つ旧state snapshotはDB migration時に読み捨てる。
        data.pop("trace_path", None)
        data["candidate_chain"] = _coerce_candidate_chain(data.get("candidate_chain"))
        if not data["candidate_chain"]:
            data["candidate_chain"] = [
                (_coalesce_optional(data.get("provider")), _coalesce_optional(data.get("model")))
            ]
        data.setdefault("qc_repair_attempts", 0)
        data.setdefault("candidate_index", 0)
        data.setdefault("candidate_checkpoint", "")
        data.setdefault("lineage_id", data.get("run_id", ""))
        data["steps"] = [StepState.from_dict(item) for item in data.get("steps", [])]
        candidate_index = int(data["candidate_index"])
        if data["candidate_chain"]:
            candidate_count = len(data["candidate_chain"])
            if candidate_index < 0:
                candidate_index = 0
            elif candidate_index >= candidate_count:
                candidate_index = max(candidate_count - 1, 0)
            data["candidate_index"] = candidate_index
        return cls(**data)

    def step(self, name: str) -> StepState:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)

    @property
    def run_path(self) -> Path:
        return Path(self.run_dir)
