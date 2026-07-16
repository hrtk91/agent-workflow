"""TUIの画面Stateと、画面ごとの入力イベント・振舞い。"""

from __future__ import annotations

import curses
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Mapping

from agent_workflow.pipeline import (
    ATTENTION_STATUSES,
    PIPELINE_FILTERS,
    PipelineItem,
    PipelineRunDetail,
    PipelineSnapshot,
    PipelineSnapshotReader,
    pipeline_items,
)


class DetailFocus(str, Enum):
    STEPS = "steps"
    LOGS = "logs"


class LogSource(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass
class TuiContext:
    """全画面で共有する依存性とread model。画面固有のcursorは持たない。"""

    reader: PipelineSnapshotReader
    refresh_seconds: float
    include_repair: bool
    filter_labels: Mapping[str, str]
    snapshot: PipelineSnapshot = field(default_factory=PipelineSnapshot.empty)
    message: str = "r:更新  m:メニュー  ::コマンド  Enter:開く  q:終了"
    last_refresh: float = 0.0
    colors_enabled: bool = False
    screen_height: int = 0


@dataclass(frozen=True)
class LogState:
    source: LogSource = LogSource.STDOUT
    follow_tail: bool = True
    offset: int = 0


@dataclass(frozen=True)
class DashboardState:
    filter_name: str = "all"
    selected_index: int = 0
    list_offset: int = 0


@dataclass(frozen=True)
class RunDetailState:
    detail: PipelineRunDetail | None
    parent: DashboardState | None = None
    step_index: int = 0
    attempt_index: int = 0
    focus: DetailFocus = DetailFocus.STEPS
    log: LogState = field(default_factory=LogState)
    content_lines: tuple[str, ...] = ()
    artifact_kind: str = "summary"
    artifact_path: str | None = None
    artifact_offset: int = 0


ScreenState = DashboardState | RunDetailState


@dataclass(frozen=True)
class BehaviorResult:
    state: ScreenState
    overlay: str | None = None
    refresh_requested: bool = False
    quit_requested: bool = False
    message: str | None = None


class UiEvent:
    """画面内の入力を意味のあるイベントへ正規化するための基底型。"""


@dataclass(frozen=True)
class Noop(UiEvent):
    pass


@dataclass(frozen=True)
class MoveSelection(UiEvent):
    delta: int


@dataclass(frozen=True)
class OpenSelected(UiEvent):
    pass


@dataclass(frozen=True)
class CycleFilter(UiEvent):
    pass


@dataclass(frozen=True)
class OpenMenu(UiEvent):
    pass


@dataclass(frozen=True)
class OpenCommand(UiEvent):
    pass


@dataclass(frozen=True)
class RefreshRequested(UiEvent):
    pass


@dataclass(frozen=True)
class QuitRequested(UiEvent):
    pass


@dataclass(frozen=True)
class NavigateDashboard(UiEvent):
    pass


@dataclass(frozen=True)
class FocusSteps(UiEvent):
    pass


@dataclass(frozen=True)
class FocusLogs(UiEvent):
    pass


@dataclass(frozen=True)
class MoveStep(UiEvent):
    delta: int


@dataclass(frozen=True)
class OpenAttempts(UiEvent):
    pass


@dataclass(frozen=True)
class ScrollLog(UiEvent):
    delta: int


@dataclass(frozen=True)
class ToggleLogSource(UiEvent):
    pass


@dataclass(frozen=True)
class SelectLogSource(UiEvent):
    source: LogSource


@dataclass(frozen=True)
class JumpLogTop(UiEvent):
    pass


@dataclass(frozen=True)
class JumpLogTail(UiEvent):
    pass


class DashboardEventPublisher:
    """dashboardのraw keyをdashboard固有イベントへ変換する。"""

    _bindings: Mapping[int, UiEvent] = {
        ord("q"): QuitRequested(),
        ord("Q"): QuitRequested(),
        curses.KEY_UP: MoveSelection(-1),
        ord("k"): MoveSelection(-1),
        curses.KEY_DOWN: MoveSelection(1),
        ord("j"): MoveSelection(1),
        10: OpenSelected(),
        13: OpenSelected(),
        ord("d"): OpenSelected(),
        ord("l"): OpenSelected(),
        ord("f"): CycleFilter(),
        ord("m"): OpenMenu(),
        ord(":"): OpenCommand(),
        ord("r"): RefreshRequested(),
    }

    def publish(self, key: int) -> UiEvent:
        return self._bindings.get(key, Noop())


class RunDetailEventPublisher:
    """run詳細のfocusに応じてraw keyを意味イベントへ変換する。"""

    _common_bindings: Mapping[int, UiEvent] = {
        27: NavigateDashboard(),
        ord("q"): NavigateDashboard(),
        ord("Q"): NavigateDashboard(),
        ord("r"): RefreshRequested(),
    }
    _focus_bindings: Mapping[DetailFocus, Mapping[int, UiEvent]] = {
        DetailFocus.STEPS: {
            ord("h"): NavigateDashboard(),
            ord("l"): FocusLogs(),
            curses.KEY_UP: MoveStep(-1),
            ord("k"): MoveStep(-1),
            curses.KEY_DOWN: MoveStep(1),
            ord("j"): MoveStep(1),
            10: OpenAttempts(),
            13: OpenAttempts(),
            ord("a"): OpenAttempts(),
        },
        DetailFocus.LOGS: {
            ord("h"): FocusSteps(),
            curses.KEY_UP: ScrollLog(-1),
            ord("k"): ScrollLog(-1),
            curses.KEY_DOWN: ScrollLog(1),
            ord("j"): ScrollLog(1),
            9: ToggleLogSource(),
            ord("o"): ToggleLogSource(),
            ord("e"): SelectLogSource(LogSource.STDERR),
            ord("g"): JumpLogTop(),
            ord("G"): JumpLogTail(),
        },
    }

    def publish(self, state: RunDetailState, key: int) -> UiEvent:
        focus_bindings = self._focus_bindings[state.focus]
        return self._common_bindings.get(key, focus_bindings.get(key, Noop()))


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
        return replace(state, selected_index=selected_index, list_offset=list_offset)

    def items(self, state: DashboardState) -> tuple[PipelineItem, ...]:
        return pipeline_items(self.context.snapshot, state.filter_name, include_jobs=False)

    def _noop(self, state: DashboardState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state)

    def _move_selection(self, state: DashboardState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, MoveSelection)
        items = self.items(state)
        selected_index = min(max(0, state.selected_index + event.delta), max(0, len(items) - 1))
        return BehaviorResult(replace(state, selected_index=selected_index))

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
            replace(state, filter_name=filter_name, selected_index=0, list_offset=0),
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
        return BehaviorResult(replace(state, focus=DetailFocus.STEPS))

    def _focus_logs(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(replace(state, focus=DetailFocus.LOGS))

    def _move_step(self, state: RunDetailState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, MoveStep)
        detail = state.detail
        if detail is None:
            return BehaviorResult(state)
        step_index = min(max(0, state.step_index + event.delta), max(0, len(detail.steps) - 1))
        return BehaviorResult(
            replace(
                state,
                step_index=step_index,
                attempt_index=latest_attempt_index(detail, step_index),
                log=replace(state.log, follow_tail=True, offset=0),
                content_lines=(),
            )
        )

    def _open_attempts(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, overlay="attempts")

    def _scroll_log(self, state: RunDetailState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, ScrollLog)
        offset = max(0, state.log.offset + event.delta)
        follow_tail = state.log.follow_tail
        if event.delta < 0:
            follow_tail = False
        elif offset >= max(0, len(state.content_lines) - 1):
            follow_tail = True
        return BehaviorResult(replace(state, log=replace(state.log, offset=offset, follow_tail=follow_tail)))

    def _toggle_log_source(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        source = LogSource.STDERR if state.log.source == LogSource.STDOUT else LogSource.STDOUT
        return BehaviorResult(replace(state, log=LogState(source=source), content_lines=()))

    def _select_log_source(self, state: RunDetailState, event: UiEvent) -> BehaviorResult:
        assert isinstance(event, SelectLogSource)
        return BehaviorResult(replace(state, log=LogState(source=event.source), content_lines=()))

    def _jump_log_top(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(replace(state, log=replace(state.log, follow_tail=False, offset=0)))

    def _jump_log_tail(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(replace(state, log=replace(state.log, follow_tail=True, offset=len(state.content_lines))))

    def _refresh(self, state: RunDetailState, _event: UiEvent) -> BehaviorResult:
        return BehaviorResult(state, refresh_requested=True)


def initial_step_index(detail: PipelineRunDetail) -> int:
    if detail.current_step:
        for index, step in enumerate(detail.steps):
            if step.name == detail.current_step:
                return index
    for index, step in enumerate(detail.steps):
        if step.status in ATTENTION_STATUSES or step.status == "running":
            return index
    return 0


def latest_attempt_index(detail: PipelineRunDetail, step_index: int) -> int:
    if step_index < 0 or step_index >= len(detail.steps):
        return 0
    step_name = detail.steps[step_index].name
    attempts = [attempt for attempt in detail.attempts if attempt.step_name == step_name]
    return max(0, len(attempts) - 1)
