from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    base_ref: str | None = None
    worktree_path: str | None = None
    summary_path: str = ""
    trace_path: str = ""
    current_step: str | None = None
    created_at: str = ""
    updated_at: str = ""
    steps: list[StepState] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        data = dict(data)
        if "takt_bin" in data and "executor_bin" not in data:
            data["executor_bin"] = data.pop("takt_bin")
        data["steps"] = [StepState.from_dict(item) for item in data.get("steps", [])]
        return cls(**data)

    def step(self, name: str) -> StepState:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)

    @property
    def run_path(self) -> Path:
        return Path(self.run_dir)
