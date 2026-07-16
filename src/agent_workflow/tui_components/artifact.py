"""artifact画面のイベント変換とBehavior。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from agent_workflow.tui.state import (
    ArtifactState,
    RunDetailState,
    TuiContext,
    dashboard_state_for,
    detail_state_for,
)

from .events import (
    JumpContentBottom,
    JumpContentTop,
    NavigateDashboard,
    NavigateDetail,
    Noop,
    RefreshRequested,
    ScrollContent,
    UiEvent,
    ArtifactEventPublisher,
)
from .result import BehaviorResult


class ArtifactBehavior:
    """summary・trace・monitor artifact画面のStateを管理する。"""

    def __init__(self, context: TuiContext) -> None:
        self.context = context
        self.events = ArtifactEventPublisher()
        self._handlers: Mapping[type[UiEvent], Callable[[ArtifactState, UiEvent], BehaviorResult]] = {
            Noop: self._noop,
            NavigateDetail: self._navigate_detail,
            NavigateDashboard: self._navigate_dashboard,
            ScrollContent: self._scroll_content,
            JumpContentTop: self._jump_top,
            JumpContentBottom: self._jump_bottom,
            RefreshRequested: self._refresh_requested,
        }

    def handle(self, state: ArtifactState, key: int) -> BehaviorResult:
        event = self.events.publish(key)
        return self._handlers[type(event)](state, event)

    def open(self, parent: RunDetailState, kind: str) -> ArtifactState:
        if parent.detail is None:
            raise ValueError("artifact画面にはrun詳細が必要です")
        return ArtifactState(detail=parent.detail, parent=parent, kind=kind)

    def refresh(self, state: ArtifactState) -> ArtifactState:
        detail = self.context.reader.run_detail(state.detail.run_id)
        return replace(state, detail=detail) if detail is not None else state

    def _noop(self, state: ArtifactState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state)

    def _navigate_detail(self, state: ArtifactState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(detail_state_for(state))

    def _navigate_dashboard(self, state: ArtifactState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(dashboard_state_for(state))

    def _scroll_content(self, state: ArtifactState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, ScrollContent)
        offset = max(0, state.offset + event.delta)
        return BehaviorResult(replace(state, offset=offset))

    def _jump_top(self, state: ArtifactState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(replace(state, offset=0))

    def _jump_bottom(self, state: ArtifactState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(replace(state, offset=len(state.content_lines)))

    def _refresh_requested(self, state: ArtifactState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, refresh_requested=True)
