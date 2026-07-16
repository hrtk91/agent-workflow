"""TUIのraw keyを画面イベントへ変換するための型。"""

from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Mapping

from agent_workflow.tui.state import DetailFocus, LogSource, RunDetailState


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


@dataclass(frozen=True)
class OpenLogsScreen(UiEvent):
    pass


@dataclass(frozen=True)
class OpenArtifactScreen(UiEvent):
    kind: str


@dataclass(frozen=True)
class MoveAttempt(UiEvent):
    delta: int


@dataclass(frozen=True)
class ScrollContent(UiEvent):
    delta: int


@dataclass(frozen=True)
class JumpContentTop(UiEvent):
    pass


@dataclass(frozen=True)
class JumpContentBottom(UiEvent):
    pass


@dataclass(frozen=True)
class NavigateDetail(UiEvent):
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


class AttemptsEventPublisher:
    """attempts画面のraw keyをイベントへ変換する。"""

    _bindings: Mapping[int, UiEvent] = {
        27: NavigateDetail(),
        ord("q"): NavigateDetail(),
        ord("Q"): NavigateDetail(),
        ord("h"): NavigateDetail(),
        curses.KEY_UP: MoveAttempt(-1),
        ord("k"): MoveAttempt(-1),
        curses.KEY_DOWN: MoveAttempt(1),
        ord("j"): MoveAttempt(1),
        10: OpenLogsScreen(),
        13: OpenLogsScreen(),
        ord("l"): OpenLogsScreen(),
        ord("s"): OpenArtifactScreen("summary"),
        ord("r"): RefreshRequested(),
    }

    def publish(self, key: int) -> UiEvent:
        return self._bindings.get(key, Noop())


class LogsEventPublisher:
    """独立ログ画面のraw keyをイベントへ変換する。"""

    _bindings: Mapping[int, UiEvent] = {
        27: NavigateDetail(),
        ord("q"): NavigateDashboard(),
        ord("Q"): NavigateDashboard(),
        ord("h"): NavigateDetail(),
        curses.KEY_UP: ScrollLog(-1),
        ord("k"): ScrollLog(-1),
        curses.KEY_DOWN: ScrollLog(1),
        ord("j"): ScrollLog(1),
        9: ToggleLogSource(),
        ord("o"): ToggleLogSource(),
        ord("e"): SelectLogSource(LogSource.STDERR),
        ord("["): MoveStep(-1),
        ord("]"): MoveStep(1),
        ord("a"): OpenAttempts(),
        ord("g"): JumpLogTop(),
        ord("G"): JumpLogTail(),
        ord("r"): RefreshRequested(),
    }

    def publish(self, key: int) -> UiEvent:
        return self._bindings.get(key, Noop())


class ArtifactEventPublisher:
    """artifact画面のraw keyをイベントへ変換する。"""

    _bindings: Mapping[int, UiEvent] = {
        27: NavigateDetail(),
        ord("q"): NavigateDashboard(),
        ord("Q"): NavigateDashboard(),
        ord("h"): NavigateDetail(),
        curses.KEY_UP: ScrollContent(-1),
        ord("k"): ScrollContent(-1),
        curses.KEY_DOWN: ScrollContent(1),
        ord("j"): ScrollContent(1),
        ord("g"): JumpContentTop(),
        ord("G"): JumpContentBottom(),
        ord("r"): RefreshRequested(),
    }

    def publish(self, key: int) -> UiEvent:
        return self._bindings.get(key, Noop())
