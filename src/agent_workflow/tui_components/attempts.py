"""attempt履歴画面のBehavior。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from agent_workflow.tui.state import (
    ArtifactState,
    AttemptsState,
    DetailFocus,
    LogsState,
    RunDetailState,
    TuiContext,
    attempts_for_step,
    detail_state_for,
)

from .events import (
    MoveAttempt,
    NavigateDetail,
    Noop,
    OpenArtifactScreen,
    OpenLogsScreen,
    RefreshRequested,
    UiEvent,
    AttemptsEventPublisher,
)
from .result import BehaviorResult


class AttemptsBehavior:
    """attempt履歴のStateと選択・画面遷移を管理する。"""

    def __init__(self, context: TuiContext) -> None:
        self.context = context
        self.events = AttemptsEventPublisher()
        self._handlers: Mapping[type[UiEvent], Callable[[AttemptsState, UiEvent], BehaviorResult]] = {
            Noop: self._noop,
            NavigateDetail: self._navigate_detail,
            MoveAttempt: self._move_attempt,
            OpenLogsScreen: self._open_logs,
            OpenArtifactScreen: self._open_artifact,
            RefreshRequested: self._refresh_requested,
        }

    def handle(self, state: AttemptsState, key: int) -> BehaviorResult:
        event = self.events.publish(key)
        return self._handlers[type(event)](state, event)

    def open(self, parent: RunDetailState) -> AttemptsState:
        """run詳細からattempts画面のStateを生成する。"""

        if parent.detail is None:
            raise ValueError("attempts画面にはrun詳細が必要です")
        return AttemptsState(
            detail=parent.detail,
            parent=parent,
            step_index=parent.step_index,
            attempt_index=parent.attempt_index,
        )

    def refresh(self, state: AttemptsState) -> AttemptsState:
        detail = self.context.reader.run_detail(state.detail.run_id)
        if detail is None:
            return state
        step_index = min(state.step_index, max(0, len(detail.steps) - 1))
        attempts = attempts_for_step(detail, step_index)
        attempt_index = min(state.attempt_index, max(0, len(attempts) - 1))
        return replace(state, detail=detail, step_index=step_index, attempt_index=attempt_index)

    def _noop(self, state: AttemptsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state)

    def _navigate_detail(self, state: AttemptsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(detail_state_for(state))

    def _move_attempt(self, state: AttemptsState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, MoveAttempt)
        attempts = attempts_for_step(state.detail, state.step_index)
        attempt_index = min(max(0, state.attempt_index + event.delta), max(0, len(attempts) - 1))
        return BehaviorResult(replace(state, attempt_index=attempt_index))

    def _open_logs(self, state: AttemptsState, _event: UiEvent) -> BehaviorResult:
        parent = replace(detail_state_for(state), focus=DetailFocus.LOGS)
        return BehaviorResult(
            LogsState(
                detail=state.detail,
                parent=parent,
                step_index=state.step_index,
                attempt_index=state.attempt_index,
            )
        )

    def _open_artifact(self, state: AttemptsState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, OpenArtifactScreen)
        return BehaviorResult(
            ArtifactState(
                detail=state.detail,
                parent=detail_state_for(state),
                kind=event.kind,
            )
        )

    def _refresh_requested(self, state: AttemptsState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, refresh_requested=True)
