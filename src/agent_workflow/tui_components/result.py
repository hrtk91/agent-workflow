"""BehaviorからTuiAppへ返す画面遷移結果。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workflow.tui.state import ScreenState


@dataclass(frozen=True)
class BehaviorResult:
    state: ScreenState
    overlay: str | None = None
    refresh_requested: bool = False
    quit_requested: bool = False
    message: str | None = None
