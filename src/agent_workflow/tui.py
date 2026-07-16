"""agent-workflowの標準ライブラリTUI。"""

from __future__ import annotations

import curses
import json
import shlex
import time
from dataclasses import dataclass, replace
from pathlib import Path

from agent_workflow.pipeline import (
    ATTENTION_STATUSES,
    PIPELINE_FILTERS,
    PipelineItem,
    PipelineAttempt,
    PipelineRun,
    PipelineRunDetail,
    PipelineStep,
    PipelineSnapshot,
    PipelineSnapshotReader,
    pipeline_items,
)
from agent_workflow.tui_components import (
    BehaviorResult,
    DashboardBehavior,
    DashboardState,
    DetailFocus,
    LogSource,
    RunDetailBehavior,
    RunDetailState,
    ScreenState,
    TuiContext,
)


FILTER_LABELS = {
    "all": "すべて",
    "running": "実行中",
    "failed": "失敗・要確認",
    "succeeded": "成功",
}
STATUS_LABELS = {
    "queued": "待機",
    "running": "実行中",
    "succeeded": "成功",
    "failed": "失敗",
    "qc_failed": "QC失敗",
    "timed_out": "タイムアウト",
    "interrupted": "中断",
    "blocked": "ブロック",
    "pending": "待機",
}
STATUS_SYMBOLS = {
    "queued": "○",
    "running": "▶",
    "succeeded": "✓",
    "failed": "✗",
    "qc_failed": "✗",
    "timed_out": "⌛",
    "interrupted": "Ⅱ",
    "blocked": "!",
    "pending": "·",
}
STATUS_COLOR_PAIRS = {
    "queued": 1,
    "pending": 1,
    "running": 2,
    "succeeded": 3,
    "failed": 4,
    "qc_failed": 4,
    "blocked": 4,
    "timed_out": 5,
    "interrupted": 6,
}
STATUS_EMOJIS = {
    "queued": "📥",
    "pending": "⏳",
    "running": "🚀",
    "succeeded": "✅",
    "failed": "❌",
    "qc_failed": "🛑",
    "timed_out": "⏱️",
    "interrupted": "⏸️",
    "blocked": "🚫",
}
STEP_LABELS = {
    "load_task": "task",
    "create_worktree": "worktree",
    "run_executor": "executor",
    "run_qc": "QC",
    "write_summary": "summary",
}
MENU_ITEMS = (
    ("filter all", "すべてのrun"),
    ("filter running", "実行中のrun"),
    ("filter failed", "失敗・要確認のrun"),
    ("filter succeeded", "成功したrun"),
    ("refresh", "今すぐ更新"),
    ("help", "操作ヘルプ"),
    ("quit", "終了"),
)
COMMAND_HELP = "filter all|running|failed|succeeded / refresh / detail / help / quit"
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_LOG_LINE_CHARS = 4_096
MAX_ARTIFACT_BYTES = 128 * 1024
MAX_CONTENT_LINES = 2_000


@dataclass(frozen=True)
class TuiCommand:
    name: str
    args: tuple[str, ...] = ()


def parse_command(raw: str) -> TuiCommand:
    """コマンドパレットの入力を副作用のないcommandへ変換する。"""

    tokens = shlex.split(raw.lstrip(":").strip())
    if not tokens:
        return TuiCommand("noop")
    aliases = {
        "f": "filter",
        "r": "refresh",
        "d": "detail",
        "a": "attempts",
        "l": "logs",
        "s": "summary",
        "t": "trace",
        "h": "help",
        "q": "quit",
    }
    name = aliases.get(tokens[0].lower(), tokens[0].lower())
    args = tuple(tokens[1:])
    if name == "filter":
        if len(args) != 1 or args[0] not in PIPELINE_FILTERS:
            raise ValueError("filterにはall、running、failed、succeededのいずれかを指定してください")
    elif name not in {"refresh", "detail", "attempts", "logs", "summary", "trace", "monitor", "help", "quit", "noop"}:
        raise ValueError(f"未知のコマンドです: {tokens[0]}")
    elif args:
        raise ValueError(f"{name}には引数を指定できません")
    return TuiCommand(name, args)


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
        self.dashboard_behavior = DashboardBehavior(self.context)
        self.detail_behavior = RunDetailBehavior(self.context)
        self.menu_index = 0
        self.command_buffer = ""
        self.menu_offset = 0
        self._legacy_content_lines: list[str] = []
        self._legacy_artifact_path: Path | None = None

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
    def view(self) -> str:
        if self._view_override is not None:
            return self._view_override
        return "detail" if isinstance(self._screen_state, RunDetailState) else "dashboard"

    @view.setter
    def view(self, value: str) -> None:
        if value == "dashboard":
            if isinstance(self._screen_state, RunDetailState):
                self._screen_state = self._screen_state.parent or DashboardState()
            self._view_override = None
            return
        if value == "detail":
            self._view_override = None
            return
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
        return self._screen_state.detail if isinstance(self._screen_state, RunDetailState) else None

    @detail.setter
    def detail(self, value: PipelineRunDetail | None) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, detail=value)

    @property
    def detail_step_index(self) -> int:
        return self._screen_state.step_index if isinstance(self._screen_state, RunDetailState) else 0

    @detail_step_index.setter
    def detail_step_index(self, value: int) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, step_index=value)

    @property
    def detail_attempt_index(self) -> int:
        return self._screen_state.attempt_index if isinstance(self._screen_state, RunDetailState) else 0

    @detail_attempt_index.setter
    def detail_attempt_index(self, value: int) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, attempt_index=value)

    @property
    def detail_focus(self) -> str:
        return self._screen_state.focus.value if isinstance(self._screen_state, RunDetailState) else "steps"

    @detail_focus.setter
    def detail_focus(self, value: str) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, focus=DetailFocus(value))

    @property
    def log_source(self) -> str:
        return self._screen_state.log.source.value if isinstance(self._screen_state, RunDetailState) else "stdout"

    @log_source.setter
    def log_source(self, value: str) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, log=replace(self._screen_state.log, source=LogSource(value)))

    @property
    def log_follow(self) -> bool:
        return self._screen_state.log.follow_tail if isinstance(self._screen_state, RunDetailState) else True

    @log_follow.setter
    def log_follow(self, value: bool) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, log=replace(self._screen_state.log, follow_tail=value))

    @property
    def content_lines(self) -> list[str]:
        if isinstance(self._screen_state, RunDetailState):
            return list(self._screen_state.content_lines)
        return self._legacy_content_lines

    @content_lines.setter
    def content_lines(self, value: list[str]) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, content_lines=tuple(value))
        else:
            self._legacy_content_lines = value

    @property
    def content_offset(self) -> int:
        if not isinstance(self._screen_state, RunDetailState):
            return 0
        if self.view == "artifact":
            return self._screen_state.artifact_offset
        return self._screen_state.log.offset

    @content_offset.setter
    def content_offset(self, value: int) -> None:
        if not isinstance(self._screen_state, RunDetailState):
            return
        if self.view == "artifact":
            self._screen_state = replace(self._screen_state, artifact_offset=value)
        else:
            self._screen_state = replace(self._screen_state, log=replace(self._screen_state.log, offset=value))

    @property
    def artifact_kind(self) -> str:
        return self._screen_state.artifact_kind if isinstance(self._screen_state, RunDetailState) else "summary"

    @artifact_kind.setter
    def artifact_kind(self, value: str) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, artifact_kind=value)

    @property
    def artifact_path(self) -> Path | None:
        if not isinstance(self._screen_state, RunDetailState) or self._screen_state.artifact_path is None:
            return None
        return Path(self._screen_state.artifact_path)

    @artifact_path.setter
    def artifact_path(self, value: Path | None) -> None:
        if isinstance(self._screen_state, RunDetailState):
            self._screen_state = replace(self._screen_state, artifact_path=str(value) if value else None)

    def run(self, screen: curses.window) -> None:
        screen.keypad(True)
        screen.timeout(200)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._init_colors()
        self.refresh()
        input_handlers = {
            "dashboard": self._handle_dashboard_input,
            "detail": self._handle_detail_input,
            "command": self._handle_command_input,
            "menu": self._handle_menu_input,
            "attempts": self._handle_attempts_input,
            "logs": self._handle_logs_input,
            "artifact": self._handle_artifact_input,
            "job": self._handle_job_input,
        }
        while True:
            if time.monotonic() - self.last_refresh >= self.refresh_seconds:
                self.refresh()
            self.draw(screen)
            key = screen.getch()
            if key == -1:
                continue
            handler = input_handlers.get(self.view, self._handle_dashboard_input)
            if handler(key):
                return

    def refresh(self) -> None:
        self.snapshot = self.reader.snapshot(include_repair=self.include_repair)
        self.last_refresh = time.monotonic()
        if isinstance(self._screen_state, DashboardState):
            self._screen_state = self.dashboard_behavior.refresh(self._screen_state)
        else:
            self._screen_state = self.detail_behavior.refresh(self._screen_state)
        if self.detail is not None and self.view in {"detail", "logs", "artifact"}:
            self._load_content()

    def draw(self, screen: curses.window) -> None:
        screen.erase()
        base_drawers = {
            "dashboard": self._draw_dashboard,
            "detail": self._draw_detail,
        }
        base_drawer = base_drawers["detail" if isinstance(self._screen_state, RunDetailState) else "dashboard"]
        base_drawer(screen)
        overlay_drawers = {
            "menu": self._draw_menu,
            "command": self._draw_command_prompt,
            "attempts": self._draw_attempts,
            "logs": self._draw_logs,
            "artifact": self._draw_artifact,
            "job": self._draw_job_detail,
        }
        overlay_drawer = overlay_drawers.get(self.view)
        if overlay_drawer is not None:
            overlay_drawer(screen)
        screen.refresh()

    def _draw_dashboard(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        items = self.items
        all_items = pipeline_items(self.snapshot, "all", include_jobs=False)
        title = f"aw runs  |  最終更新: {compact_timestamp(self.snapshot.generated_at)}"
        self._add(screen, 0, 0, title, width - 1, curses.A_BOLD)
        self._add(
            screen,
            1,
            0,
            f"run: {len(all_items)}  実行中: {sum(item.status == 'running' for item in all_items)}"
            f"  失敗・要確認: {sum(item.status in ATTENTION_STATUSES for item in all_items)}"
            f"  成功: {sum(item.status == 'succeeded' for item in all_items)}",
            width - 1,
        )
        if height < 8:
            self._add(screen, 3, 0, "端末の高さが足りません。ウィンドウを広げてください。", width - 1)
            return
        self._draw_filter_tabs(screen, 0, 2, width - 1)
        list_top = 5
        list_bottom = max(list_top + 1, height - 3)
        list_capacity = max(0, list_bottom - list_top)
        self._ensure_selection_visible(list_capacity)
        list_label = "run履歴"
        if len(items) > list_capacity:
            first = self.list_offset + 1
            last = min(len(items), self.list_offset + list_capacity)
            list_label += f" ({first}-{last}/{len(items)})"
        self._add(screen, 3, 0, list_label, width - 1, curses.A_UNDERLINE)
        self._add(screen, 4, 0, "  開始日時              経過       run                  状態       step", width - 1, curses.A_DIM)
        visible_items = items[self.list_offset : self.list_offset + list_capacity]
        for row, item in enumerate(visible_items):
            absolute_index = self.list_offset + row
            attr = curses.A_REVERSE if absolute_index == self.selected_index else 0
            self._draw_item(screen, list_top + row, item, width - 1, attr)
        if not items:
            self._add(screen, list_top, 0, "表示対象のrunはありません", width - 1)

        self._add(screen, height - 2, 0, self.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:選択  Enter/l:詳細  f:filter  m:menu  ::コマンド  q:終了", width - 1)

    def _draw_filter_tabs(self, screen: curses.window, x: int, y: int, width: int) -> None:
        cursor = x
        for filter_name in PIPELINE_FILTERS:
            label = f" {FILTER_LABELS[filter_name]} "
            remaining = width - (cursor - x)
            if remaining <= 0:
                break
            attr = curses.A_REVERSE if filter_name == self.filter_name else curses.A_DIM
            self._add(screen, y, cursor, label, remaining, attr)
            cursor += len(label) + 1

    def _draw_detail(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        detail = self.detail
        self._add(screen, 0, 0, "📋 run詳細", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        if height < 10:
            self._add(screen, 2, 0, "端末の高さが足りません。ウィンドウを広げてください。", width - 1)
            return

        elapsed = format_duration(detail.elapsed_seconds)
        self._add(
            screen,
            1,
            0,
            f"{status_emoji(detail.status)} {detail.run_id}  {status_label(detail.status)}  経過={elapsed}",
            width - 1,
            self._status_attr(detail.status) | curses.A_BOLD,
        )
        self._add(
            screen,
            2,
            0,
            f"開始: {compact_timestamp(detail.created_at)}  終了: {compact_timestamp(detail.finished_at)}",
            width - 1,
        )
        self._add(
            screen,
            3,
            0,
            f"📁 {detail.repo_path}  🔧 {detail.workflow}  🎯 {detail.current_step or '-'}",
            width - 1,
            curses.A_DIM,
        )

        section_y = 5
        left_width = max(32, min(54, width // 2))
        right_x = left_width + 3
        right_width = max(1, width - right_x - 1)
        left_attr = curses.A_UNDERLINE | curses.A_BOLD
        right_attr = curses.A_UNDERLINE | curses.A_BOLD
        if self.detail_focus == "steps":
            left_attr |= curses.A_REVERSE
        else:
            right_attr |= curses.A_REVERSE
        self._add(screen, section_y, 0, "🧩 Pipeline / step", left_width - 1, left_attr)
        self._add(screen, section_y, right_x, "📝 選択stepのログ", right_width, right_attr)
        step_capacity = max(1, height - section_y - 3)
        for index, step in enumerate(detail.steps[:step_capacity]):
            row = section_y + 1 + index
            selected = index == self.detail_step_index
            self._draw_step_row(screen, row, 0, step, left_width - 1, selected)
        self._draw_detail_logs(screen, right_x, section_y + 1, right_width, height - section_y - 3)
        self._add(screen, height - 2, 0, self.message, width - 1, curses.A_DIM)
        footer = (
            "↑↓/jk:step  l:ログへ  h:一覧へ  Enter/a:試行  Esc:一覧"
            if self.detail_focus == "steps"
            else "↑↓/jk:スクロール  g/G:先頭/末尾  h:stepへ  Tab/o/e:stdout/stderr  r:更新  Esc:一覧"
        )
        self._add(screen, height - 1, 0, footer, width - 1)

    def _draw_detail_logs(self, screen: curses.window, x: int, y: int, width: int, height: int) -> None:
        step = self.selected_detail_step
        if step is None:
            self._add(screen, y, x, "ログ対象のstepはありません。", width)
            return
        attempt = self.selected_attempt
        attempt_label = f"#{attempt.attempt}" if attempt else "current"
        self._add(
            screen,
            y,
            x,
            f"{status_emoji(step.status)} {STEP_LABELS.get(step.name, step.name)} / {attempt_label} / {self.log_source}"
            f"  {'📡末尾追従' if self.log_follow else '📍履歴位置'}",
            width,
            self._status_attr(step.status) | curses.A_BOLD,
        )
        self._add(screen, y + 1, x, f"path: {self.selected_log_path or '(なし)'}", width, curses.A_DIM)
        visible = max(1, height - 2)
        max_offset = max(0, len(self.content_lines) - visible)
        if self.log_follow:
            self.content_offset = max_offset
        else:
            self.content_offset = min(max(0, self.content_offset), max_offset)
        if not self.content_lines:
            self._add(screen, y + 2, x, "(ログはありません)", width)
            return
        for index, line in enumerate(self.content_lines[self.content_offset : self.content_offset + visible]):
            self._add(screen, y + 2 + index, x, line, width)

    def _draw_attempts(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        detail = self.detail
        self._add(screen, 0, 0, "🧾 step試行履歴", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        step = self.selected_detail_step
        if step is None:
            self._add(screen, 2, 0, "選択中のstepはありません。Escで戻る。", width - 1)
            return
        attempts = self.selected_step_attempts
        self._add(screen, 1, 0, f"{detail.run_id} / {STEP_LABELS.get(step.name, step.name)}", width - 1, curses.A_UNDERLINE)
        if not attempts:
            self._add(screen, 3, 0, "このstepの試行履歴はありません。", width - 1)
        else:
            list_width = max(38, min(64, width // 2))
            self._add(screen, 3, 0, "試行", list_width - 1, curses.A_UNDERLINE)
            for index, attempt in enumerate(attempts[: max(1, height - 8)]):
                selected = index == self.detail_attempt_index
                line = (
                    f"{status_emoji(attempt.status)} #{attempt.attempt}  {status_label(attempt.status)}"
                    f"  {format_duration(attempt.duration_seconds)}"
                )
                if attempt.exit_code is not None:
                    line += f"  exit={attempt.exit_code}"
                attr = self._status_attr(attempt.status) | (curses.A_REVERSE if selected else 0)
                self._add(screen, 4 + index, 0, line, list_width - 1, attr)
            right_x = list_width + 2
            self._add(screen, 3, right_x, "選択試行", max(1, width - right_x - 1), curses.A_UNDERLINE)
            self._draw_attempt_detail(screen, attempts[self.detail_attempt_index], right_x, 4, max(1, width - right_x - 1), height - 7)
        self._add(screen, height - 2, 0, self.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:試行  Enter/l:ログ  s:summary  Esc:runへ", width - 1)

    def _draw_logs(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        detail = self.detail
        self._add(screen, 0, 0, "📝 ログビューア", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        step = self.selected_detail_step
        if step is None:
            self._add(screen, 2, 0, "ログ対象のstepはありません。Escで戻る。", width - 1)
            return
        attempt = self.selected_attempt
        attempt_label = f"#{attempt.attempt}" if attempt else "current"
        path = self.selected_log_path
        self._add(
            screen,
            1,
            0,
            f"{detail.run_id} / {STEP_LABELS.get(step.name, step.name)} / {attempt_label} / {self.log_source}",
            width - 1,
            curses.A_UNDERLINE,
        )
        self._add(screen, 2, 0, f"path: {path or '(なし)'}", width - 1, curses.A_DIM)
        visible = max(1, height - 5)
        max_offset = max(0, len(self.content_lines) - visible)
        if self.log_follow:
            self.content_offset = max_offset
        else:
            self.content_offset = min(max(0, self.content_offset), max_offset)
        for index, line in enumerate(self.content_lines[self.content_offset : self.content_offset + visible]):
            self._add(screen, 3 + index, 0, line, width - 1)
        self._add(screen, height - 2, 0, self.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:スクロール  g/G:先頭/末尾  Tab/o/e:stdout/stderr  [/]:step  a:試行  r:更新  Esc", width - 1)

    def _draw_artifact(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        detail = self.detail
        self._add(screen, 0, 0, f"📄 {artifact_label(self.artifact_kind)}", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        self._add(screen, 1, 0, f"{detail.run_id} / {self.artifact_path or '(なし)'}", width - 1, curses.A_UNDERLINE)
        visible = max(1, height - 4)
        max_offset = max(0, len(self.content_lines) - visible)
        self.content_offset = min(max(0, self.content_offset), max_offset)
        for index, line in enumerate(self.content_lines[self.content_offset : self.content_offset + visible]):
            self._add(screen, 3 + index, 0, line, width - 1)
        self._add(screen, height - 2, 0, self.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:スクロール  g/G:先頭/末尾  r:更新  Esc:runへ", width - 1)

    def _draw_job_detail(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        item = self.selected_item
        self._add(screen, 0, 0, "📥 queue jobワークスペース", width - 1, curses.A_BOLD)
        if item is None or item.job is None:
            self._add(screen, 2, 0, "選択中のjob詳細はありません。Escで一覧へ戻る。", width - 1)
            return
        job = item.job
        lines = [
            (2, f"{status_emoji(job.status)} status: {job.status} ({status_label(job.status)})", self._status_attr(job.status) | curses.A_BOLD),
            (3, f"🆔 job_id: {job.job_id}", 0),
            (4, f"📁 repo: {job.repo_path}", 0),
            (5, f"🔧 workflow: {job.workflow}", 0),
            (6, f"🎯 purpose: {job.purpose}", 0),
            (7, f"🕒 created: {job.created_at}", curses.A_DIM),
            (8, f"🕒 updated: {job.updated_at}", curses.A_DIM),
            (9, f"📄 summary: {job.summary_path or '(未作成)'}", 0),
        ]
        for row, line, attr in lines:
            self._add(screen, row, 0, line, width - 1, attr)
        if job.error:
            self._add(screen, 11, 0, f"❌ error: {job.error}", width - 1, self._status_attr("failed"))
        elif job.run_id:
            self._add(screen, 11, 0, f"🔗 run_id: {job.run_id}", width - 1)
        else:
            self._add(screen, 11, 0, "このjobはまだrun開始前です。", width - 1)
        self._add(screen, height - 1, 0, "Esc/q:一覧へ  r:更新", width - 1)

    def _draw_step_row(self, screen: curses.window, row: int, x: int, step: PipelineStep, width: int, selected: bool) -> None:
        marker = "▶" if selected else status_emoji(step.status)
        label = STEP_LABELS.get(step.name, step.name)
        line = f"{marker} {label}  {status_label(step.status)}  {format_duration(step.duration_seconds)}  試行={step.attempts}"
        if step.error:
            line += f"  {step.error}"
        attr = self._status_attr(step.status) | (curses.A_REVERSE if selected else 0)
        if selected:
            attr |= curses.A_BOLD
        self._add(screen, row, x, line, width, attr)

    def _draw_attempt_detail(self, screen: curses.window, attempt: PipelineAttempt, x: int, y: int, width: int, height: int) -> None:
        lines = [
            (f"{status_emoji(attempt.status)} #{attempt.attempt}  {status_label(attempt.status)}", self._status_attr(attempt.status) | curses.A_BOLD),
            (f"開始: {attempt.started_at or '-'}", 0),
            (f"終了: {attempt.finished_at or '(実行中)' }", 0),
            (f"経過: {format_duration(attempt.duration_seconds)}", 0),
            (f"exit: {attempt.exit_code if attempt.exit_code is not None else '-'}", 0),
            (f"failure: {attempt.failure_category or '-'}", 0),
            (f"stdout: {attempt.stdout_path or '(なし)'}", curses.A_DIM),
            (f"stderr: {attempt.stderr_path or '(なし)'}", curses.A_DIM),
        ]
        if attempt.error:
            lines.append((f"❌ {attempt.error}", self._status_attr("failed")))
        for index, (line, attr) in enumerate(lines[: max(0, height)]):
            self._add(screen, y + index, x, line, width, attr)

    def _draw_menu(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        self._screen_height = height
        menu_width = min(max(42, max(len(label) for _, label in MENU_ITEMS) + 8), max(1, width - 2))
        menu_height = min(len(MENU_ITEMS) + 2, max(1, height - 2))
        x = max(0, (width - menu_width) // 2)
        y = max(0, (height - menu_height) // 2)
        visible_count = max(0, menu_height - 2)
        self._ensure_menu_visible(visible_count)
        try:
            screen.addstr(y, x, "┌" + "─" * max(0, menu_width - 2) + "┐")
            for row in range(1, menu_height - 1):
                screen.addstr(y + row, x, "│" + " " * max(0, menu_width - 2) + "│")
            screen.addstr(y + menu_height - 1, x, "└" + "─" * max(0, menu_width - 2) + "┘")
        except curses.error:
            return
        for row, index in enumerate(range(self.menu_offset, min(len(MENU_ITEMS), self.menu_offset + visible_count))):
            _, label = MENU_ITEMS[index]
            attr = curses.A_REVERSE if index == self.menu_index else 0
            self._add(screen, y + 1 + row, x + 2, f"{index + 1}. {label}", menu_width - 4, attr)

    def _draw_command_prompt(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        self._add(screen, height - 2, 0, ":" + self.command_buffer, width - 1, curses.A_BOLD)

    def _handle_dashboard_input(self, key: int) -> bool:
        if not isinstance(self._screen_state, DashboardState):
            return False
        result = self.dashboard_behavior.handle(self._screen_state, key)
        self._apply_behavior_result(result)
        if result.overlay == "menu":
            self.menu_index = 0
            self.menu_offset = 0
        elif result.overlay == "command":
            self.command_buffer = ""
        return result.quit_requested

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
        elif isinstance(self._screen_state, RunDetailState) and self.view in {"detail", "logs"}:
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
        handlers = {
            "detail": self._handle_detail_input,
            "attempts": self._handle_attempts_input,
            "logs": self._handle_logs_input,
            "artifact": self._handle_artifact_input,
            "job": self._handle_job_input,
        }
        return handlers.get(self.view, self._handle_dashboard_input)(key)

    def _handle_detail_input(self, key: int) -> bool:
        if not isinstance(self._screen_state, RunDetailState):
            return False
        result = self.detail_behavior.handle(self._screen_state, key)
        self._apply_behavior_result(result)
        return result.quit_requested

    def _handle_attempts_input(self, key: int) -> bool:
        if key in (27, ord("q"), ord("Q")):
            self.view = "detail"
        elif key in (curses.KEY_UP, ord("k")):
            self.detail_attempt_index = max(0, self.detail_attempt_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.detail_attempt_index = min(max(0, len(self.selected_step_attempts) - 1), self.detail_attempt_index + 1)
        elif key in (10, 13, ord("l")):
            self._open_logs()
        elif key == ord("s"):
            self._open_artifact("summary")
        elif key == ord("r"):
            self.refresh()
            self.message = "試行履歴を更新しました。"
        return False

    def _handle_logs_input(self, key: int) -> bool:
        if key == 27:
            self.view = "detail"
        elif key in (ord("q"), ord("Q")):
            self.view = "dashboard"
        elif key in (curses.KEY_UP, ord("k")):
            self._scroll_log(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._scroll_log(1)
        elif key in (9, ord("o")):
            self.log_source = "stderr" if self.log_source == "stdout" else "stdout"
            self.log_follow = True
            self._load_content()
        elif key == ord("e"):
            self.log_source = "stderr"
            self.log_follow = True
            self._load_content()
        elif key == ord("["):
            self.detail_step_index = max(0, self.detail_step_index - 1)
            self._select_latest_attempt()
            self.log_follow = True
            self._load_content()
        elif key == ord("]"):
            self.detail_step_index = min(max(0, len(self.detail_steps) - 1), self.detail_step_index + 1)
            self._select_latest_attempt()
            self.log_follow = True
            self._load_content()
        elif key == ord("a"):
            self._open_attempts()
        elif key == ord("g"):
            self.log_follow = False
            self.content_offset = 0
        elif key == ord("G"):
            self.log_follow = True
            self.content_offset = len(self.content_lines)
        elif key == ord("r"):
            self.refresh()
            self.message = "ログを更新しました。"
        return False

    def _handle_artifact_input(self, key: int) -> bool:
        if key == 27:
            self.view = "detail"
        elif key in (ord("q"), ord("Q")):
            self.view = "dashboard"
        elif key in (curses.KEY_UP, ord("k")):
            self.content_offset = max(0, self.content_offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.content_offset += 1
        elif key == ord("g"):
            self.content_offset = 0
        elif key == ord("G"):
            self.content_offset = len(self.content_lines)
        elif key == ord("r"):
            self._load_content()
            self.message = f"{artifact_label(self.artifact_kind)}を更新しました。"
        return False

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
        if not isinstance(self._screen_state, DashboardState):
            return
        result = self.dashboard_behavior.handle(self._screen_state, ord("l"))
        self._apply_behavior_result(result)

    def _open_attempts(self) -> None:
        if self.detail is None:
            self._open_selected_item()
        if self.detail is not None:
            self._select_latest_attempt()
            self.view = "attempts"

    def _open_logs(self) -> None:
        if self.detail is None:
            self._open_selected_item()
        if self.detail is not None:
            self.view = "logs"
            self.log_follow = True
            self.content_offset = 0
            self._load_content()

    def _open_artifact(self, kind: str) -> None:
        if self.detail is None:
            self._open_selected_item()
        if self.detail is None:
            return
        self.artifact_kind = kind
        self.view = "artifact"
        self.content_offset = 0
        self._load_content()

    def _load_content(self) -> None:
        if self.view in {"detail", "logs"}:
            path = Path(self.selected_log_path) if self.selected_log_path else None
            self.content_lines = tail_file_lines(path, limit=MAX_CONTENT_LINES)
            return
        self.artifact_path = find_artifact_path(self.detail, self.artifact_kind)
        self.content_lines = read_artifact_lines(self.artifact_path)

    def _scroll_log(self, delta: int) -> None:
        if delta < 0:
            self.log_follow = False
        self.content_offset = max(0, self.content_offset + delta)
        if delta > 0 and self.content_offset >= max(0, len(self.content_lines) - 1):
            self.log_follow = True

    def _initial_step_index(self, detail: PipelineRunDetail) -> int:
        if detail.current_step:
            for index, step in enumerate(detail.steps):
                if step.name == detail.current_step:
                    return index
        for index, step in enumerate(detail.steps):
            if step.status in ATTENTION_STATUSES or step.status == "running":
                return index
        return 0

    def _select_latest_attempt(self) -> None:
        attempts = self.selected_step_attempts
        self.detail_attempt_index = max(0, len(attempts) - 1)

    def _clamp_detail_selection(self) -> None:
        self.detail_step_index = min(self.detail_step_index, max(0, len(self.detail_steps) - 1))
        self.detail_attempt_index = min(self.detail_attempt_index, max(0, len(self.selected_step_attempts) - 1))

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

    def _item_line(self, item: PipelineItem) -> str:
        identifier = item.item_id[-20:]
        if item.run is None:
            return f"{status_symbol(item.status)} {compact_timestamp(item.updated_at)}  {identifier}  {status_label(item.status)}"
        step = current_step(item.run)
        step_label = STEP_LABELS.get(step.name, step.name) if step is not None else "-"
        return (
            f"{status_symbol(item.status)} {compact_timestamp(item.run.created_at)}"
            f"  {format_duration(item.run.elapsed_seconds):>8}  {identifier:<20}"
            f"  {status_label(item.status):<8}  {step_label}"
        )

    def _draw_item(self, screen: curses.window, y: int, item: PipelineItem, width: int, attr: int) -> None:
        line = self._item_line(item)
        symbol = status_symbol(item.status)
        self._add(screen, y, 0, symbol, 2, attr | self._status_attr(item.status))
        self._add(screen, y, 2, line[len(symbol) + 1 :], width - 2, attr)

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
            for pair, color in {
                1: curses.COLOR_BLUE,
                2: curses.COLOR_CYAN,
                3: curses.COLOR_GREEN,
                4: curses.COLOR_RED,
                5: curses.COLOR_MAGENTA,
                6: curses.COLOR_YELLOW,
            }.items():
                curses.init_pair(pair, color, -1)
            self.colors_enabled = True
        except curses.error:
            self.colors_enabled = False

    def _status_attr(self, status: str) -> int:
        if not self.colors_enabled:
            return 0
        return curses.color_pair(STATUS_COLOR_PAIRS.get(status, 0))

    @staticmethod
    def _add(screen: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        height, screen_width = screen.getmaxyx()
        if y < 0 or y >= height or x >= screen_width or width <= 0:
            return
        try:
            screen.addnstr(y, max(0, x), text, min(width, screen_width - max(0, x) - 1), attr)
        except curses.error:
            pass


def current_step(run: PipelineRun) -> PipelineStep | None:
    if run.current_step:
        for step in run.steps:
            if step.name == run.current_step:
                return step
    for step in run.steps:
        if step.status in ATTENTION_STATUSES or step.status == "running":
            return step
    return next(
        (step for step in run.steps if step.status != "succeeded"),
        next((step for step in reversed(run.steps) if step.stdout_path or step.stderr_path), None),
    )


def tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0:
        return []
    try:
        if not path.is_file():
            return ["(ファイルがありません)"]
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            offset = max(0, size - MAX_LOG_TAIL_BYTES)
            stream.seek(offset)
            data = stream.read(size - offset)
        lines = data.decode("utf-8", errors="replace").splitlines()[-limit:]
        return [truncate_log_line(line) for line in lines] or ["(空)"]
    except OSError as exc:
        return [f"(読み込み失敗: {exc})"]


def tail_file_lines(path: Path | None, limit: int) -> list[str]:
    """ログビューア向けに、ファイル末尾を bounded read して行列へ変換する。"""

    if limit <= 0:
        return []
    if path is None:
        return ["(ログはありません)"]
    try:
        if not path.is_file():
            return ["(ファイルがありません)"]
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            offset = max(0, size - MAX_LOG_TAIL_BYTES)
            stream.seek(offset)
            data = stream.read(size - offset)
        lines = data.decode("utf-8", errors="replace").splitlines()[-limit:]
        return [truncate_log_line(line) for line in lines] or ["(空)"]
    except OSError as exc:
        return [f"(読み込み失敗: {exc})"]


def read_artifact_lines(path: Path | None) -> list[str]:
    """summary/trace/monitorを表示用にbounded readする。"""

    if path is None:
        return ["(成果物がありません)"]
    try:
        if not path.is_file():
            return [f"(ファイルがありません: {path})"]
        raw = path.read_bytes()
        truncated = len(raw) > MAX_ARTIFACT_BYTES
        text = raw[:MAX_ARTIFACT_BYTES].decode("utf-8", errors="replace")
        if path.suffix == ".json" and not truncated:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        lines = [truncate_log_line(line) for line in text.splitlines()]
        if truncated:
            lines.append(f"… {MAX_ARTIFACT_BYTES} bytesまで表示。残りは省略しました")
        return lines[:MAX_CONTENT_LINES] or ["(空)"]
    except OSError as exc:
        return [f"(読み込み失敗: {exc})"]


def find_artifact_path(detail: PipelineRunDetail | None, kind: str) -> Path | None:
    if detail is None:
        return None
    if kind == "summary":
        return Path(detail.summary_path) if detail.summary_path else None
    filename = {"trace": "trace.md", "monitor": "monitor.json"}.get(kind)
    if filename is None:
        return None
    roots: list[Path] = []
    if detail.summary_path:
        roots.append(Path(detail.summary_path).parent / "executor_observability" / "takt")
    if detail.worktree_path:
        roots.append(Path(detail.worktree_path) / ".takt" / "runs")
    candidates: list[Path] = []
    for root in roots:
        try:
            candidates.extend(root.glob(f"*/{filename}"))
        except OSError:
            continue
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def artifact_label(kind: str) -> str:
    return {"summary": "summary", "trace": "executor trace", "monitor": "executor monitor"}.get(kind, kind)


def truncate_log_line(line: str) -> str:
    if len(line) <= MAX_LOG_LINE_CHARS:
        return line
    return line[: MAX_LOG_LINE_CHARS - 1] + "…"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def status_emoji(status: str) -> str:
    return STATUS_EMOJIS.get(status, "🔹")


def status_symbol(status: str) -> str:
    return STATUS_SYMBOLS.get(status, "?")


def compact_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ", 1)[:19]


def format_duration(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"
