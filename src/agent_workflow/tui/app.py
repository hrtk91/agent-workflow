"""TUIの画面ホスト。"""

from __future__ import annotations

import curses
import time
from dataclasses import replace
from pathlib import Path

from agent_workflow.pipeline import (
    PipelineItem,
    PipelineAttempt,
    PipelineRunDetail,
    PipelineStep,
    PipelineSnapshot,
    PipelineSnapshotReader,
    pipeline_items,
)
from agent_workflow.tui_components import (
    ArtifactState,
    AttemptsState,
    BehaviorResult,
    DashboardState,
    DetailFocus,
    LogSource,
    LogsState,
    RunDetailState,
    ScreenController,
    ScreenControllers,
    ScreenState,
    TuiContext,
    detail_state_for,
)

from .commands import TuiCommand, parse_command
from .constants import COMMAND_HELP, FILTER_LABELS, MENU_ITEMS, STATUS_COLOR_PAIRS, STEP_LABELS
from .content import (
    MAX_CONTENT_LINES,
    artifact_label,
    compact_timestamp,
    current_step,
    find_artifact_path,
    format_duration,
    read_artifact_lines,
    status_emoji,
    status_label,
    status_symbol,
    tail_file_lines,
)
from .rendering import TuiRenderer


def run_tui(state_dir: Path, *, refresh_seconds: float = 1.0, include_repair: bool = False) -> None:
    if refresh_seconds <= 0:
        raise ValueError("--refresh-secondsは0より大きくしてください")
    reader = PipelineSnapshotReader(state_dir.expanduser() / "jobs.sqlite")
    app = TuiApp(reader, refresh_seconds=refresh_seconds, include_repair=include_repair)
    curses.wrapper(app.run)


class TuiApp:
    def __init__(self, reader: PipelineSnapshotReader, *, refresh_seconds: float, include_repair: bool) -> None:
        self.context = TuiContext(
            reader=reader,
            refresh_seconds=refresh_seconds,
            include_repair=include_repair,
            filter_labels=FILTER_LABELS,
        )
        self._screen_state: ScreenState = DashboardState()
        self._view_override: str | None = None
        self.screen_controllers = ScreenControllers(self.context)
        # 既存のテスト・拡張向けにBehavior属性は互換維持する。
        self.dashboard_behavior = self.screen_controllers.dashboard.behavior
        self.detail_behavior = self.screen_controllers.detail.behavior
        self.attempts_behavior = self.screen_controllers.attempts.behavior
        self.logs_behavior = self.screen_controllers.logs.behavior
        self.artifact_behavior = self.screen_controllers.artifact.behavior
        self.renderer = TuiRenderer(self)
        self.menu_index = 0
        self.command_buffer = ""
        self.menu_offset = 0
        self._legacy_content_lines: list[str] = []

    @property
    def reader(self) -> PipelineSnapshotReader:
        return self.context.reader

    @property
    def refresh_seconds(self) -> float:
        return self.context.refresh_seconds

    @property
    def include_repair(self) -> bool:
        return self.context.include_repair

    @property
    def snapshot(self) -> PipelineSnapshot:
        return self.context.snapshot

    @snapshot.setter
    def snapshot(self, value: PipelineSnapshot) -> None:
        self.context.snapshot = value

    @property
    def message(self) -> str:
        return self.context.message

    @message.setter
    def message(self, value: str) -> None:
        self.context.message = value

    @property
    def last_refresh(self) -> float:
        return self.context.last_refresh

    @last_refresh.setter
    def last_refresh(self, value: float) -> None:
        self.context.last_refresh = value

    @property
    def colors_enabled(self) -> bool:
        return self.context.colors_enabled

    @colors_enabled.setter
    def colors_enabled(self, value: bool) -> None:
        self.context.colors_enabled = value

    @property
    def _screen_height(self) -> int:
        return self.context.screen_height

    @_screen_height.setter
    def _screen_height(self, value: int) -> None:
        self.context.screen_height = value

    @property
    def screen_controller(self) -> ScreenController:
        return self.screen_controllers.resolve(self._screen_state)

    @property
    def view(self) -> str:
        if self._view_override is not None:
            return self._view_override
        return self.screen_controller.view

    @view.setter
    def view(self, value: str) -> None:
        next_state = self.screen_controllers.state_for_view(
            self._screen_state,
            value,
            artifact_kind=self.artifact_kind,
        )
        if next_state is not None:
            self._screen_state = next_state
            self._view_override = None
        else:
            self._view_override = value

    @property
    def filter_name(self) -> str:
        return self._screen_state.filter_name if isinstance(self._screen_state, DashboardState) else "all"

    @filter_name.setter
    def filter_name(self, value: str) -> None:
        if isinstance(self._screen_state, DashboardState):
            self._screen_state = replace(self._screen_state, filter_name=value)

    @property
    def selected_index(self) -> int:
        return self._screen_state.selected_index if isinstance(self._screen_state, DashboardState) else 0

    @selected_index.setter
    def selected_index(self, value: int) -> None:
        if isinstance(self._screen_state, DashboardState):
            self._screen_state = replace(self._screen_state, selected_index=value)

    @property
    def list_offset(self) -> int:
        return self._screen_state.list_offset if isinstance(self._screen_state, DashboardState) else 0

    @list_offset.setter
    def list_offset(self, value: int) -> None:
        if isinstance(self._screen_state, DashboardState):
            self._screen_state = replace(self._screen_state, list_offset=value)

    @property
    def detail(self) -> PipelineRunDetail | None:
        return detail_state_for(self._screen_state).detail

    @detail.setter
    def detail(self, value: PipelineRunDetail | None) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, detail=value)
        elif isinstance(self._screen_state, AttemptsState) and value is not None:
            self._screen_state = replace(self._screen_state, detail=value)
        elif isinstance(self._screen_state, LogsState) and value is not None:
            self._screen_state = replace(self._screen_state, detail=value)
        elif isinstance(self._screen_state, ArtifactState) and value is not None:
            self._screen_state = replace(self._screen_state, detail=value)

    @property
    def detail_step_index(self) -> int:
        if isinstance(self._screen_state, (AttemptsState, LogsState)):
            return self._screen_state.step_index
        return detail_state_for(self._screen_state).step_index

    @detail_step_index.setter
    def detail_step_index(self, value: int) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, step_index=value)
        elif isinstance(self._screen_state, (AttemptsState, LogsState)):
            self._screen_state = replace(self._screen_state, step_index=value)

    @property
    def detail_attempt_index(self) -> int:
        if isinstance(self._screen_state, (AttemptsState, LogsState)):
            return self._screen_state.attempt_index
        return detail_state_for(self._screen_state).attempt_index

    @detail_attempt_index.setter
    def detail_attempt_index(self, value: int) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, attempt_index=value)
        elif isinstance(self._screen_state, (AttemptsState, LogsState)):
            self._screen_state = replace(self._screen_state, attempt_index=value)

    @property
    def detail_focus(self) -> str:
        return detail_state_for(self._screen_state).focus.value

    @detail_focus.setter
    def detail_focus(self, value: str) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, focus=DetailFocus(value))
        elif isinstance(self._screen_state, (AttemptsState, LogsState, ArtifactState)):
            parent = replace(self._screen_state.parent, focus=DetailFocus(value))
            self._screen_state = replace(self._screen_state, parent=parent)

    @property
    def log_source(self) -> str:
        if isinstance(self._screen_state, (RunDetailState, LogsState)):
            return self._screen_state.log.source.value
        return "stdout"

    @log_source.setter
    def log_source(self, value: str) -> None:
        if isinstance(self._screen_state, (RunDetailState, LogsState)):
            self._screen_state = replace(self._screen_state, log=replace(self._screen_state.log, source=LogSource(value)))

    @property
    def log_follow(self) -> bool:
        if isinstance(self._screen_state, (RunDetailState, LogsState)):
            return self._screen_state.log.follow_tail
        return True

    @log_follow.setter
    def log_follow(self, value: bool) -> None:
        if isinstance(self._screen_state, (RunDetailState, LogsState)):
            self._screen_state = replace(self._screen_state, log=replace(self._screen_state.log, follow_tail=value))

    @property
    def content_lines(self) -> list[str]:
        if isinstance(self._screen_state, (RunDetailState, LogsState, ArtifactState)):
            return list(self._screen_state.content_lines)
        return self._legacy_content_lines

    @content_lines.setter
    def content_lines(self, value: list[str]) -> None:
        if isinstance(self._screen_state, (RunDetailState, LogsState, ArtifactState)):
            self._screen_state = replace(self._screen_state, content_lines=tuple(value))
        else:
            self._legacy_content_lines = value

    @property
    def content_offset(self) -> int:
        if isinstance(self._screen_state, ArtifactState):
            return self._screen_state.offset
        if isinstance(self._screen_state, (RunDetailState, LogsState)):
            return self._screen_state.log.offset
        return 0

    @content_offset.setter
    def content_offset(self, value: int) -> None:
        if isinstance(self._screen_state, ArtifactState):
            self._screen_state = replace(self._screen_state, offset=value)
        elif isinstance(self._screen_state, (RunDetailState, LogsState)):
            self._screen_state = replace(self._screen_state, log=replace(self._screen_state.log, offset=value))

    @property
    def artifact_kind(self) -> str:
        if isinstance(self._screen_state, ArtifactState):
            return self._screen_state.kind
        return "summary"

    @artifact_kind.setter
    def artifact_kind(self, value: str) -> None:
        if isinstance(self._screen_state, ArtifactState):
            self._screen_state = replace(self._screen_state, kind=value)

    @property
    def artifact_path(self) -> Path | None:
        if isinstance(self._screen_state, ArtifactState):
            return Path(self._screen_state.path) if self._screen_state.path else None
        return None

    @artifact_path.setter
    def artifact_path(self, value: Path | None) -> None:
        if isinstance(self._screen_state, ArtifactState):
            self._screen_state = replace(self._screen_state, path=str(value) if value else None)

    def run(self, screen: curses.window) -> None:
        screen.keypad(True)
        screen.timeout(200)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.renderer.init_colors()
        self.refresh()
        input_handlers = {
            "command": self._handle_command_input,
            "menu": self._handle_menu_input,
            "job": self._handle_job_input,
        }
        while True:
            if time.monotonic() - self.last_refresh >= self.refresh_seconds:
                self.refresh()
            self.draw(screen)
            key = screen.getch()
            if key == -1:
                continue
            handler = input_handlers.get(self.view, self._handle_screen_input)
            if handler(key):
                return

    def refresh(self) -> None:
        self.snapshot = self.reader.snapshot(include_repair=self.include_repair)
        self.last_refresh = time.monotonic()
        self._screen_state = self.screen_controller.refresh(self._screen_state)
        if self.detail is not None and self.view in {"detail", "logs", "artifact"}:
            self._load_content()

    def draw(self, screen: curses.window) -> None:
        self.renderer.draw(screen)

    def _handle_screen_input(self, key: int) -> bool:
        result = self.screen_controller.handle(self._screen_state, key)
        self._apply_behavior_result(result)
        if result.overlay == "menu":
            self.menu_index = 0
            self.menu_offset = 0
        elif result.overlay == "command":
            self.command_buffer = ""
        return result.quit_requested

    def _handle_dashboard_input(self, key: int) -> bool:
        """旧呼び出し元向けの互換wrapper。実処理はControllerへ委譲する。"""

        if not isinstance(self._screen_state, DashboardState):
            return False
        return self._handle_screen_input(key)

    def _cycle_filter(self) -> None:
        result = self.dashboard_behavior.handle(self._screen_state, ord("f"))
        self._apply_behavior_result(result)

    def _apply_behavior_result(self, result: BehaviorResult) -> None:
        """BehaviorのState遷移を画面hostへ反映する。"""

        self._screen_state = result.state
        self._view_override = result.overlay
        if result.message:
            self.message = result.message
        if result.refresh_requested:
            self.refresh()
        elif self.view in {"detail", "logs", "artifact"}:
            self._load_content()

    def _handle_menu_input(self, key: int) -> bool:
        if key in (27, ord("m")):
            self.view = "dashboard"
        elif key in (curses.KEY_UP, ord("k")):
            self.menu_index = max(0, self.menu_index - 1)
            self._ensure_menu_visible(max(0, self._menu_visible_count()))
        elif key in (curses.KEY_DOWN, ord("j")):
            self.menu_index = min(len(MENU_ITEMS) - 1, self.menu_index + 1)
            self._ensure_menu_visible(max(0, self._menu_visible_count()))
        elif key in (10, 13):
            return self._apply_command(MENU_ITEMS[self.menu_index][0])
        elif ord("1") <= key <= ord(str(min(9, len(MENU_ITEMS)))):
            self.menu_index = key - ord("1")
            return self._apply_command(MENU_ITEMS[self.menu_index][0])
        return False

    def _handle_command_input(self, key: int) -> bool:
        if key == 27:
            self.view = "dashboard"
        elif key in (10, 13):
            raw = self.command_buffer
            self.view = "dashboard"
            try:
                command = parse_command(raw)
            except ValueError as exc:
                self.message = str(exc)
            else:
                return self._apply_command_obj(command)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.command_buffer = self.command_buffer[:-1]
        elif 0 <= key < 256:
            self.command_buffer += chr(key)
        return False

    def _handle_workspace_input(self, key: int) -> bool:
        """旧呼び出し元向けに、従来のview単位の振り分けを維持する。"""

        match self.view:
            case "dashboard":
                return self._handle_dashboard_input(key)
            case "detail":
                return self._handle_detail_input(key)
            case "attempts":
                return self._handle_attempts_input(key)
            case "logs":
                return self._handle_logs_input(key)
            case "artifact":
                return self._handle_artifact_input(key)
            case "job":
                return self._handle_job_input(key)
            case _:
                return self._handle_dashboard_input(key)

    def _handle_detail_input(self, key: int) -> bool:
        if not isinstance(self._screen_state, RunDetailState):
            return False
        return self._handle_screen_input(key)

    def _handle_attempts_input(self, key: int) -> bool:
        if not isinstance(self._screen_state, AttemptsState):
            return False
        return self._handle_screen_input(key)

    def _handle_logs_input(self, key: int) -> bool:
        if not isinstance(self._screen_state, LogsState):
            return False
        return self._handle_screen_input(key)

    def _handle_artifact_input(self, key: int) -> bool:
        if not isinstance(self._screen_state, ArtifactState):
            return False
        return self._handle_screen_input(key)

    def _handle_job_input(self, key: int) -> bool:
        if key in (27, ord("q"), ord("Q")):
            self.view = "dashboard"
        elif key == ord("r"):
            self.refresh()
            self.message = "job詳細を更新しました。"
        return False

    def _apply_command(self, raw: str) -> bool:
        try:
            command = parse_command(raw)
        except ValueError as exc:
            self.message = str(exc)
            self.view = "dashboard"
            return False
        return self._apply_command_obj(command)

    def _apply_command_obj(self, command: TuiCommand) -> bool:
        if command.name == "quit":
            return True
        if command.name == "filter":
            self.filter_name = command.args[0]
            self.selected_index = 0
            self.list_offset = 0
            self.message = f"絞り込みを変更しました: {FILTER_LABELS[self.filter_name]}"
        elif command.name == "refresh":
            self.refresh()
            self.message = "更新しました。"
        elif command.name == "detail":
            self._open_selected_item()
        elif command.name == "attempts":
            self._open_selected_item()
            if self.detail is not None:
                self._open_attempts()
        elif command.name == "logs":
            self._open_selected_item()
            if self.detail is not None:
                self.detail_focus = "logs"
        elif command.name in {"summary", "trace", "monitor"}:
            self._open_selected_item()
            if self.detail is not None:
                self._open_artifact(command.name)
        elif command.name == "help":
            self.message = COMMAND_HELP
        if command.name not in {"detail", "attempts", "logs", "summary", "trace", "monitor"}:
            self.view = "dashboard"
        return False

    @property
    def items(self) -> tuple[PipelineItem, ...]:
        return pipeline_items(self.snapshot, self.filter_name, include_jobs=False)

    @property
    def selected_item(self) -> PipelineItem | None:
        items = self.items
        return items[self.selected_index] if items and self.selected_index < len(items) else None

    def _clamp_selection(self) -> None:
        self.selected_index = min(self.selected_index, max(0, len(self.items) - 1))

    def _ensure_selection_visible(self, capacity: int) -> None:
        item_count = len(self.items)
        if capacity <= 0 or item_count == 0:
            self.list_offset = 0
            return
        self.list_offset = min(self.list_offset, max(0, item_count - capacity))
        if self.selected_index < self.list_offset:
            self.list_offset = self.selected_index
        elif self.selected_index >= self.list_offset + capacity:
            self.list_offset = self.selected_index - capacity + 1

    @property
    def detail_steps(self) -> tuple[PipelineStep, ...]:
        return self.detail.steps if self.detail is not None else ()

    @property
    def selected_detail_step(self) -> PipelineStep | None:
        steps = self.detail_steps
        return steps[self.detail_step_index] if steps and self.detail_step_index < len(steps) else None

    @property
    def selected_step_attempts(self) -> tuple[PipelineAttempt, ...]:
        step = self.selected_detail_step
        if self.detail is None or step is None:
            return ()
        return tuple(attempt for attempt in self.detail.attempts if attempt.step_name == step.name)

    @property
    def selected_attempt(self) -> PipelineAttempt | None:
        attempts = self.selected_step_attempts
        return attempts[self.detail_attempt_index] if attempts and self.detail_attempt_index < len(attempts) else None

    @property
    def selected_log_path(self) -> str | None:
        attempt = self.selected_attempt
        step = self.selected_detail_step
        if attempt is not None:
            attempt_path = attempt.stdout_path if self.log_source == "stdout" else attempt.stderr_path
            if attempt_path:
                return attempt_path
        if step is None:
            return None
        return step.stdout_path if self.log_source == "stdout" else step.stderr_path

    def _open_selected_item(self) -> None:
        if self.screen_controller is not self.screen_controllers.dashboard:
            return
        result = self.screen_controllers.dashboard.handle(self._screen_state, ord("l"))
        self._apply_behavior_result(result)

    def _open_attempts(self) -> None:
        if self.detail is None:
            self._open_selected_item()
        if self.detail is not None:
            result = self.screen_controllers.open_attempts(self._screen_state)
            if result.state is not self._screen_state:
                self._apply_behavior_result(result)

    def _open_logs(self) -> None:
        if self.detail is None:
            self._open_selected_item()
        if self.detail is not None:
            result = self.screen_controllers.open_logs(self._screen_state)
            if result.state is not self._screen_state:
                self._apply_behavior_result(result)

    def _open_artifact(self, kind: str) -> None:
        if self.detail is None:
            self._open_selected_item()
        artifact_state = self.screen_controllers.open_artifact(self._screen_state, kind)
        if artifact_state is None:
            return
        self._screen_state = artifact_state
        self._load_content()

    def _load_content(self) -> None:
        if self.view in {"detail", "logs"}:
            path = Path(self.selected_log_path) if self.selected_log_path else None
            self.content_lines = tail_file_lines(path, limit=MAX_CONTENT_LINES)
            return
        if self.view != "artifact":
            return
        self.artifact_path = find_artifact_path(self.detail, self.artifact_kind)
        self.content_lines = read_artifact_lines(self.artifact_path)

    def _menu_visible_count(self) -> int:
        return max(0, self._screen_height - 2) if hasattr(self, "_screen_height") else len(MENU_ITEMS)

    def _ensure_menu_visible(self, capacity: int) -> None:
        if capacity <= 0:
            self.menu_offset = 0
            return
        self.menu_offset = min(self.menu_offset, max(0, len(MENU_ITEMS) - capacity))
        if self.menu_index < self.menu_offset:
            self.menu_offset = self.menu_index
        elif self.menu_index >= self.menu_offset + capacity:
            self.menu_offset = self.menu_index - capacity + 1
