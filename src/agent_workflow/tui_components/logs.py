"""独立ログ画面のBehavior。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from agent_workflow.tui.state import (
    AttemptsState,
    LogSource,
    LogState,
    LogsState,
    RunDetailState,
    TuiContext,
    attempts_for_step,
    detail_state_for,
    latest_attempt_index,
    dashboard_state_for,
    scroll_log as scroll_log_state,
)

from .events import (
    MoveStep,
    NavigateDashboard,
    NavigateDetail,
    Noop,
    OpenAttempts,
    RefreshRequested,
    ScrollLog,
    SelectLogSource,
    ToggleLogSource,
    JumpLogTail,
    JumpLogTop,
    UiEvent,
    LogsEventPublisher,
)
from .result import BehaviorResult


class LogsBehavior:
    """独立ログ画面のStateとログcursorを管理する。"""

    def __init__(self, context: TuiContext) -> None:
        self.context = context
        self.events = LogsEventPublisher()
        self._handlers: Mapping[type[UiEvent], Callable[[LogsState, UiEvent], BehaviorResult]] = {
            Noop: self._noop,
            NavigateDetail: self._navigate_detail,
            NavigateDashboard: self._navigate_dashboard,
            ScrollLog: self._scroll_log,
            ToggleLogSource: self._toggle_log_source,
            SelectLogSource: self._select_log_source,
            MoveStep: self._move_step,
            OpenAttempts: self._open_attempts,
            JumpLogTop: self._jump_log_top,
            JumpLogTail: self._jump_log_tail,
            RefreshRequested: self._refresh_requested,
        }

    def handle(self, state: LogsState, key: int) -> BehaviorResult:
        event = self.events.publish(key)
        return self._handlers[type(event)](state, event)

    def open(self, parent: RunDetailState) -> LogsState:
        """run詳細から独立ログ画面のStateを生成する。"""

        if parent.detail is None:
            raise ValueError("ログ画面にはrun詳細が必要です")
        return LogsState(
            detail=parent.detail,
            parent=parent.focus_logs(),
            step_index=parent.step_index,
            attempt_index=parent.attempt_index,
        )

    def refresh(self, state: LogsState) -> LogsState:
        detail = self.context.reader.run_detail(state.detail.run_id)
        if detail is None:
            return state
        step_index = min(state.step_index, max(0, len(detail.steps) - 1))
        attempt_index = min(state.attempt_index, max(0, len(attempts_for_step(detail, step_index)) - 1))
        return replace(state, detail=detail, step_index=step_index, attempt_index=attempt_index)

    def _noop(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state)

    def _navigate_detail(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(detail_state_for(state))

    def _navigate_dashboard(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(dashboard_state_for(state))

    def _scroll_log(self, state: LogsState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, ScrollLog)
        return BehaviorResult(scroll_log_state(state, event.delta))

    def _toggle_log_source(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        source = LogSource.STDERR if state.log.source == LogSource.STDOUT else LogSource.STDOUT
        return BehaviorResult(state.select_log_source(source))

    def _select_log_source(self, state: LogsState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, SelectLogSource)
        return BehaviorResult(state.select_log_source(event.source))

    def _move_step(self, state: LogsState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, MoveStep)
        step_index = min(max(0, state.step_index + event.delta), max(0, len(state.detail.steps) - 1))
        return BehaviorResult(
            replace(
                state,
                step_index=step_index,
                attempt_index=latest_attempt_index(state.detail, step_index),
                log=LogState(),
                content_lines=(),
            )
        )

    def _open_attempts(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(
            AttemptsState(
                detail=state.detail,
                parent=detail_state_for(state),
                step_index=state.step_index,
                attempt_index=state.attempt_index,
            )
        )

    def _jump_log_top(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.jump_log_top())

    def _jump_log_tail(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state.jump_log_tail())

    def _refresh_requested(self, state: LogsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, refresh_requested=True)
