"""dashboard画面のイベント変換とBehavior。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from agent_workflow.pipeline import PIPELINE_FILTERS, PipelineItem, pipeline_items
from agent_workflow.tui.state import (
    DashboardState,
    TuiContext,
    RunDetailState,
    initial_step_index,
    latest_attempt_index,
    move_selection as move_selection_state,
)

from .events import (
    CycleFilter,
    MoveSelection,
    Noop,
    OpenCommand,
    OpenMenu,
    OpenSelected,
    QuitRequested,
    RefreshRequested,
    UiEvent,
    DashboardEventPublisher,
)
from .result import BehaviorResult


class DashboardBehavior:
    """dashboardのStateをイベントに応じて更新する。"""

    def __init__(self, context: TuiContext) -> None:
        self.context = context
        self.events = DashboardEventPublisher()
        self._handlers: Mapping[type[UiEvent], Callable[[DashboardState, UiEvent], BehaviorResult]] = {
            Noop: self._noop,
            MoveSelection: self._move_selection,
            OpenSelected: self._open_selected,
            CycleFilter: self._cycle_filter,
            OpenMenu: self._open_menu,
            OpenCommand: self._open_command,
            RefreshRequested: self._refresh,
            QuitRequested: self._quit,
        }

    def handle(self, state: DashboardState, key: int) -> BehaviorResult:
        event = self.events.publish(key)
        return self._handlers[type(event)](state, event)

    def refresh(self, state: DashboardState) -> DashboardState:
        items = self.items(state)
        selected_index = min(state.selected_index, max(0, len(items) - 1))
        list_offset = min(state.list_offset, max(0, len(items) - 1))
        return replace(
            state.select_index(selected_index),
            list_offset=list_offset,
        )

    def items(self, state: DashboardState) -> tuple[PipelineItem, ...]:
        return pipeline_items(self.context.snapshot, state.filter_name, include_jobs=False)

    def _noop(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state)

    def _move_selection(self, state: DashboardState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, MoveSelection)
        return BehaviorResult(
            move_selection_state(
                state,
                event.delta,
                item_count=len(self.items(state)),
            )
        )

    def _open_selected(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        items = self.items(state)
        item = items[state.selected_index] if items and state.selected_index < len(items) else None
        if item is None or item.run is None:
            return BehaviorResult(state, message="一覧からrunを選択してください。")
        detail = self.context.reader.run_detail(item.run.run_id)
        if detail is None:
            return BehaviorResult(state, message=f"run詳細を読み込めません: {item.run.run_id}")
        step_index = initial_step_index(detail)
        return BehaviorResult(
            RunDetailState(
                detail=detail,
                parent=state,
                step_index=step_index,
                attempt_index=latest_attempt_index(detail, step_index),
            )
        )

    def _cycle_filter(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        index = PIPELINE_FILTERS.index(state.filter_name)
        filter_name = PIPELINE_FILTERS[(index + 1) % len(PIPELINE_FILTERS)]
        return BehaviorResult(
            state.change_filter(filter_name),
            message=f"絞り込みを変更しました: {self.context.filter_labels[filter_name]}",
        )

    def _open_menu(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, overlay="menu")

    def _open_command(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, overlay="command")

    def _refresh(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, refresh_requested=True)

    def _quit(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, quit_requested=True)
