"""TUIの共有Contextと、画面Stateの定義。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Protocol, Self, TypeVar, assert_never

from agent_workflow.pipeline import (
    ATTENTION_STATUSES,
    PIPELINE_FILTERS,
    PipelineAttempt,
    PipelineRunDetail,
    PipelineSnapshot,
    PipelineSnapshotReader,
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

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("ログoffsetは0以上である必要があります")

    def reset_to_tail(self) -> Self:
        return replace(self, follow_tail=True, offset=0)

    def scroll(self, delta: int, *, content_length: int) -> Self:
        if content_length < 0:
            raise ValueError("ログ長は0以上である必要があります")

        max_offset = max(0, content_length - 1)
        offset = max(0, self.offset + delta)
        follow_tail = self.follow_tail
        if delta < 0:
            follow_tail = False
        elif offset >= max_offset:
            follow_tail = True
        return replace(self, offset=offset, follow_tail=follow_tail)

    def select_source(self, source: LogSource) -> Self:
        return type(self)(source=source)

    def jump_top(self) -> Self:
        return replace(self, follow_tail=False, offset=0)

    def jump_tail(self, *, content_length: int) -> Self:
        if content_length < 0:
            raise ValueError("ログ長は0以上である必要があります")
        return replace(self, follow_tail=True, offset=content_length)


class SelectableState(Protocol):
    """一覧選択を持つStateが提供する更新操作。"""

    @property
    def selected_index(self) -> int:
        ...

    def move_selection(self, delta: int, *, item_count: int) -> Self:
        ...

    def select_index(self, index: int) -> Self:
        ...


class LogViewportState(Protocol):
    """ログ表示位置を持つStateが提供する更新操作。"""

    @property
    def log(self) -> LogState:
        ...

    @property
    def content_lines(self) -> tuple[str, ...]:
        ...

    def scroll_log(self, delta: int) -> Self:
        ...

    def select_log_source(self, source: LogSource) -> Self:
        ...

    def jump_log_top(self) -> Self:
        ...

    def jump_log_tail(self) -> Self:
        ...


SelectableStateT = TypeVar("SelectableStateT", bound=SelectableState)
LogViewportStateT = TypeVar("LogViewportStateT", bound=LogViewportState)


def move_selection(
    state: SelectableStateT,
    delta: int,
    *,
    item_count: int,
) -> SelectableStateT:
    """SelectableStateの更新契約を保ったまま選択を移動する。"""

    return state.move_selection(delta, item_count=item_count)


def scroll_log(state: LogViewportStateT, delta: int) -> LogViewportStateT:
    """LogViewportStateの更新契約を保ったままログをスクロールする。"""

    return state.scroll_log(delta)


@dataclass(frozen=True)
class DashboardState:
    filter_name: str = "all"
    selected_index: int = 0
    list_offset: int = 0

    def __post_init__(self) -> None:
        if self.filter_name not in PIPELINE_FILTERS:
            raise ValueError(f"不正なfilterです: {self.filter_name}")
        if self.selected_index < 0:
            raise ValueError("選択indexは0以上である必要があります")
        if self.list_offset < 0:
            raise ValueError("一覧offsetは0以上である必要があります")

    def move_selection(self, delta: int, *, item_count: int) -> Self:
        if item_count < 0:
            raise ValueError("一覧件数は0以上である必要があります")
        max_index = max(0, item_count - 1)
        selected_index = min(
            max(0, self.selected_index + delta),
            max_index,
        )
        return replace(self, selected_index=selected_index)

    def select_index(self, index: int) -> Self:
        if index < 0:
            raise ValueError("選択indexは0以上である必要があります")
        return replace(self, selected_index=index)

    def change_filter(self, filter_name: str) -> Self:
        if filter_name not in PIPELINE_FILTERS:
            raise ValueError(f"不正なfilterです: {filter_name}")
        return replace(self, filter_name=filter_name, selected_index=0, list_offset=0)


@dataclass(frozen=True)
class RunDetailState:
    detail: PipelineRunDetail | None
    parent: DashboardState | None = None
    step_index: int = 0
    attempt_index: int = 0
    focus: DetailFocus = DetailFocus.STEPS
    log: LogState = field(default_factory=LogState)
    content_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.step_index < 0:
            raise ValueError("step indexは0以上である必要があります")
        if self.attempt_index < 0:
            raise ValueError("attempt indexは0以上である必要があります")

    def move_step(self, delta: int) -> Self:
        if self.detail is None or not self.detail.steps:
            return self

        max_index = len(self.detail.steps) - 1
        step_index = min(max(0, self.step_index + delta), max_index)
        return replace(
            self,
            step_index=step_index,
            attempt_index=latest_attempt_index(self.detail, step_index),
            log=self.log.reset_to_tail(),
            content_lines=(),
        )

    def focus_steps(self) -> Self:
        return replace(self, focus=DetailFocus.STEPS)

    def focus_logs(self) -> Self:
        return replace(self, focus=DetailFocus.LOGS)

    def scroll_log(self, delta: int) -> Self:
        return replace(
            self,
            log=self.log.scroll(delta, content_length=len(self.content_lines)),
        )

    def select_log_source(self, source: LogSource) -> Self:
        return replace(self, log=self.log.select_source(source), content_lines=())

    def jump_log_top(self) -> Self:
        return replace(self, log=self.log.jump_top())

    def jump_log_tail(self) -> Self:
        return replace(
            self,
            log=self.log.jump_tail(content_length=len(self.content_lines)),
        )


@dataclass(frozen=True)
class AttemptsState:
    detail: PipelineRunDetail
    parent: RunDetailState
    step_index: int
    attempt_index: int

    def __post_init__(self) -> None:
        if self.step_index < 0:
            raise ValueError("step indexは0以上である必要があります")
        if self.attempt_index < 0:
            raise ValueError("attempt indexは0以上である必要があります")


@dataclass(frozen=True)
class LogsState:
    detail: PipelineRunDetail
    parent: RunDetailState
    step_index: int
    attempt_index: int
    log: LogState = field(default_factory=LogState)
    content_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.step_index < 0:
            raise ValueError("step indexは0以上である必要があります")
        if self.attempt_index < 0:
            raise ValueError("attempt indexは0以上である必要があります")

    def scroll_log(self, delta: int) -> Self:
        return replace(
            self,
            log=self.log.scroll(delta, content_length=len(self.content_lines)),
        )

    def select_log_source(self, source: LogSource) -> Self:
        return replace(self, log=self.log.select_source(source), content_lines=())

    def jump_log_top(self) -> Self:
        return replace(self, log=self.log.jump_top())

    def jump_log_tail(self) -> Self:
        return replace(
            self,
            log=self.log.jump_tail(content_length=len(self.content_lines)),
        )


@dataclass(frozen=True)
class ArtifactState:
    detail: PipelineRunDetail
    parent: RunDetailState
    kind: str
    path: str | None = None
    content_lines: tuple[str, ...] = ()
    offset: int = 0

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("artifact offsetは0以上である必要があります")


ScreenState = DashboardState | RunDetailState | AttemptsState | LogsState | ArtifactState


def attempts_for_step(detail: PipelineRunDetail, step_index: int) -> tuple[PipelineAttempt, ...]:
    if step_index < 0 or step_index >= len(detail.steps):
        return ()
    step_name = detail.steps[step_index].name
    return tuple(attempt for attempt in detail.attempts if attempt.step_name == step_name)


def detail_state_for(state: ScreenState) -> RunDetailState:
    match state:
        case RunDetailState() as detail:
            return detail
        case AttemptsState() as attempts:
            return replace(
                attempts.parent,
                detail=attempts.detail,
                step_index=attempts.step_index,
                attempt_index=attempts.attempt_index,
            )
        case LogsState() as logs:
            return replace(
                logs.parent,
                detail=logs.detail,
                step_index=logs.step_index,
                attempt_index=logs.attempt_index,
                focus=DetailFocus.LOGS,
                log=logs.log,
                content_lines=logs.content_lines,
            )
        case ArtifactState() as artifact:
            return replace(artifact.parent, detail=artifact.detail)
        case DashboardState():
            return RunDetailState(detail=None)
        case _ as unreachable:
            assert_never(unreachable)


def dashboard_state_for(state: ScreenState) -> DashboardState:
    match state:
        case DashboardState() as dashboard:
            return dashboard
        case _:
            return detail_state_for(state).parent or DashboardState()


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
