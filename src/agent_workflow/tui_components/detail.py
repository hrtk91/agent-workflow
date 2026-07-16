"""run詳細画面のイベント変換とBehavior。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from agent_workflow.tui.state import (
    AttemptsState,
    DashboardState,
    LogSource,
    RunDetailState,
    TuiContext,
    latest_attempt_index,
    scroll_log as scroll_log_state,
)

from .events import (
    FocusLogs,
    FocusSteps,
    JumpLogTail,
    JumpLogTop,
    MoveStep,
    NavigateDashboard,
    Noop,
    OpenAttempts,
    RefreshRequested,
    ScrollLog,
    SelectLogSource,
    ToggleLogSource,
    UiEvent,
    RunDetailEventPublisher,
)
from .result import BehaviorResult


class RunDetailBehavior:
    """run詳細のStateを更新し、ログ追従とfocusを管理する。"""

    def __init__(self, context: TuiContext) -> None:
        self.context = context
        self.events = RunDetailEventPublisher()
        self._handlers: Mapping[type[UiEvent], Callable[[RunDetailState, UiEvent], BehaviorResult]] = {
            Noop: self._noop,
            NavigateDashboard: self._navigate_dashboard,
            FocusSteps: self._focus_steps,
            FocusLogs: self._focus_logs,
            MoveStep: self._move_step,
            OpenAttempts: self._open_attempts,
            ScrollLog: self._scroll_log,
            ToggleLogSource: self._toggle_log_source,
            SelectLogSource: self._select_log_source,
            JumpLogTop: self._jump_log_top,
            JumpLogTail: self._jump_log_tail,
            RefreshRequested: self._refresh,
        }

    def handle(self, state: RunDetailState, key: int) -> BehaviorResult:
        event = self.events.publish(state, key)
        return self._handlers[type(event)](state, event)

    def refresh(self, state: RunDetailState) -> RunDetailState:
        if state.detail is None:
            return state
        detail = self.context.reader.run_detail(state.detail.run_id)
        if detail is None:
            return state
        step_index = min(state.step_index, max(0, len(detail.steps) - 1))
        attempt_index = min(latest_attempt_index(detail, step_index), state.attempt_index)
        return replace(state, detail=detail, step_index=step_index, attempt_index=attempt_index)

    def _noop(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state)

    def _navigate_dashboard(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.parent or DashboardState())

    def _focus_steps(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.focus_steps())

    def _focus_logs(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.focus_logs())

    def _move_step(self, state: RunDetailState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, MoveStep)
        return BehaviorResult(state.move_step(event.delta))

    def _open_attempts(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        detail = state.detail
        if detail is None:
            return BehaviorResult(state)
        return BehaviorResult(
            AttemptsState(
                detail=detail,
                parent=state,
                step_index=state.step_index,
                attempt_index=state.attempt_index,
            )
        )

    def _scroll_log(self, state: RunDetailState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, ScrollLog)
        return BehaviorResult(scroll_log_state(state, event.delta))

    def _toggle_log_source(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        source = LogSource.STDERR if state.log.source == LogSource.STDOUT else LogSource.STDOUT
        return BehaviorResult(state.select_log_source(source))

    def _select_log_source(self, state: RunDetailState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, SelectLogSource)
        return BehaviorResult(state.select_log_source(event.source))

    def _jump_log_top(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.jump_log_top())

    def _jump_log_tail(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.jump_log_tail())

    def _refresh(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, refresh_requested=True)
