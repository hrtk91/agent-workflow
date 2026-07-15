"""agent-workflowの標準ライブラリTUI。"""

from __future__ import annotations

import curses
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from agent_workflow.pipeline import (
    ATTENTION_STATUSES,
    PIPELINE_FILTERS,
    PipelineItem,
    PipelineRun,
    PipelineStep,
    PipelineSnapshot,
    PipelineSnapshotReader,
    pipeline_items,
)


FILTER_LABELS = {
    "all": "すべて",
    "running": "実行中",
    "queued": "キュー",
    "attention": "要確認",
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
    ("filter queued", "キューのジョブ"),
    ("filter attention", "要確認のrun"),
    ("filter succeeded", "成功したrun"),
    ("refresh", "今すぐ更新"),
    ("detail", "選択中runの詳細"),
    ("logs", "選択中runのログ末尾"),
    ("quit", "終了"),
)
COMMAND_HELP = "filter all|running|queued|attention|succeeded / refresh / detail / logs / help / quit"
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_LOG_LINE_CHARS = 4_096


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
        "l": "logs",
        "h": "help",
        "q": "quit",
    }
    name = aliases.get(tokens[0].lower(), tokens[0].lower())
    args = tuple(tokens[1:])
    if name == "filter":
        if len(args) != 1 or args[0] not in PIPELINE_FILTERS:
            raise ValueError("filterにはall、running、queued、attention、succeededのいずれかを指定してください")
    elif name not in {"refresh", "detail", "logs", "help", "quit", "noop"}:
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
        self.reader = reader
        self.refresh_seconds = refresh_seconds
        self.include_repair = include_repair
        self.snapshot = PipelineSnapshot.empty()
        self.filter_name = "all"
        self.selected_index = 0
        self.list_offset = 0
        self.colors_enabled = False
        self.view = "dashboard"
        self.menu_index = 0
        self.command_buffer = ""
        self.message = "r:更新  m:メニュー  ::コマンド  Enter:詳細  q:終了"
        self.last_refresh = 0.0

    def run(self, screen: curses.window) -> None:
        screen.keypad(True)
        screen.timeout(200)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._init_colors()
        self.refresh()
        while True:
            if time.monotonic() - self.last_refresh >= self.refresh_seconds:
                self.refresh()
            self.draw(screen)
            key = screen.getch()
            if key == -1:
                continue
            if self.view == "command":
                if self._handle_command_input(key):
                    return
            elif self.view == "menu":
                if self._handle_menu_input(key):
                    return
            elif self.view in {"detail", "logs"}:
                if self._handle_detail_input(key):
                    return
            elif self._handle_dashboard_input(key):
                return

    def refresh(self) -> None:
        self.snapshot = self.reader.snapshot(include_repair=self.include_repair)
        self.last_refresh = time.monotonic()
        self._clamp_selection()

    def draw(self, screen: curses.window) -> None:
        screen.erase()
        if self.view == "dashboard" or self.view in {"command", "menu"}:
            self._draw_dashboard(screen)
        elif self.view == "detail":
            self._draw_detail(screen)
        else:
            self._draw_logs(screen)
        if self.view == "menu":
            self._draw_menu(screen)
        elif self.view == "command":
            self._draw_command_prompt(screen)
        screen.refresh()

    def _draw_dashboard(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        items = self.items
        selected = self.selected_item
        title = f"aw pipeline  |  絞り込み: {FILTER_LABELS[self.filter_name]}  |  {self.snapshot.generated_at}"
        self._add(screen, 0, 0, title, width - 1, curses.A_BOLD)
        self._add(
            screen,
            1,
            0,
            f"queue: {len(self.snapshot.jobs)}  runs: {len(self.snapshot.runs)}  表示: {len(items)}",
            width - 1,
        )
        if height < 8:
            self._add(screen, 3, 0, "端末の高さが足りません。ウィンドウを広げてください。", width - 1)
            return
        list_top = 3
        list_bottom = max(list_top + 2, height // 2)
        left_width = max(28, min(42, width // 3))
        list_capacity = max(0, list_bottom - list_top)
        self._ensure_selection_visible(list_capacity)
        if width > left_width + 2:
            try:
                screen.vline(list_top, left_width, curses.ACS_VLINE, max(1, list_bottom - list_top))
            except curses.error:
                pass
        list_label = "一覧"
        if len(items) > list_capacity:
            first = self.list_offset + 1
            last = min(len(items), self.list_offset + list_capacity)
            list_label += f" ({first}-{last}/{len(items)})"
        self._add(screen, 2, 0, list_label, left_width - 1, curses.A_UNDERLINE)
        visible_items = items[self.list_offset : self.list_offset + list_capacity]
        for row, item in enumerate(visible_items):
            absolute_index = self.list_offset + row
            attr = curses.A_REVERSE if absolute_index == self.selected_index else 0
            self._draw_item(screen, list_top + row, item, left_width - 1, attr)
        if not items:
            self._add(screen, list_top, 0, "表示対象はありません", left_width - 1)

        right_x = left_width + 2
        right_width = max(1, width - right_x - 1)
        self._add(screen, 2, right_x, "パイプライン", right_width, curses.A_UNDERLINE)
        if selected is None:
            self._add(screen, 4, right_x, "一覧からrunまたはjobを選択してください。", right_width)
        elif selected.run is not None:
            self._draw_pipeline(screen, selected.run, right_x, 4, right_width)
        else:
            self._add(screen, 4, right_x, f"job status: {status_label(selected.status)}", right_width)
            if selected.job and selected.job.error:
                self._add(screen, 5, right_x, f"error: {selected.job.error}", right_width)
            elif selected.status == "queued":
                self._add(screen, 5, right_x, "このjobはまだrun開始前です。", right_width)
            self._add(screen, 7, right_x, f"job: {selected.item_id}", right_width)
            self._add(screen, 8, right_x, f"repo: {selected.repo_path}", right_width)
            self._add(screen, 9, right_x, f"workflow: {selected.workflow}", right_width)

        message_y = max(list_bottom + 1, height - 2)
        self._add(screen, message_y, 0, self.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk 選択  Enter 詳細  m メニュー  : コマンド  q 終了", width - 1)

    def _draw_pipeline(self, screen: curses.window, run: PipelineRun, x: int, y: int, width: int) -> None:
        self._add(
            screen,
            y,
            x,
            f"{run.run_id}  {status_label(run.status)}",
            width,
            curses.A_BOLD | self._status_attr(run.status),
        )
        self._add(screen, y + 1, x, f"repo: {run.repo_path}", width)
        self._add(screen, y + 2, x, f"workflow: {run.workflow}  QC修復: {run.qc_repair_attempts}", width)
        self._draw_pipeline_flow(screen, run, x, y + 4, width)
        active_step_name = active_step(run)
        for index, step in enumerate(run.steps):
            is_active = step.name == active_step_name
            marker = "▶" if is_active else status_symbol(step.status)
            detail = f"{marker} {STEP_LABELS.get(step.name, step.name)}: {status_label(step.status)}"
            if step.attempts:
                detail += f"  試行={step.attempts}"
            if step.duration_seconds is not None:
                detail += f"  {step.duration_seconds:.1f}s"
            if step.error:
                detail += f"  {step.error}"
            attr = self._status_attr(step.status) | (curses.A_BOLD if is_active else 0)
            self._add(screen, y + 6 + index, x, detail, width, attr)

    def _draw_pipeline_flow(self, screen: curses.window, run: PipelineRun, x: int, y: int, width: int) -> None:
        active_step_name = active_step(run)
        cursor = x
        right_edge = x + width
        for index, step in enumerate(run.steps):
            label = STEP_LABELS.get(step.name, step.name)
            segment = f" {label} {status_symbol(step.status)} "
            if cursor < right_edge:
                is_active = step.name == active_step_name
                attr = self._status_attr(step.status) | (curses.A_BOLD if is_active else 0)
                if is_active:
                    attr |= curses.A_REVERSE
                self._add(screen, y, cursor, segment, right_edge - cursor, attr)
            cursor += len(segment)
            if index < len(run.steps) - 1:
                connector = " → "
                self._add(screen, y, cursor, connector, max(0, right_edge - cursor), curses.A_DIM)
                cursor += len(connector)

    def _draw_detail(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        item = self.selected_item
        self._add(screen, 0, 0, "📋 run詳細", width - 1, curses.A_BOLD)
        if item is None or item.run is None:
            self._add(screen, 2, 0, "選択中のrun詳細はありません。Escで戻る。", width - 1)
            return
        run = item.run
        active_step_name = active_step(run)
        metadata = (
            (2, f"🆔 run_id: {run.run_id}", 0),
            (3, f"{status_emoji(run.status)} status: {run.status} ({status_label(run.status)})", self._status_attr(run.status) | curses.A_BOLD),
            (4, f"🎯 current_step: {run.current_step or '-'}", 0),
            (5, f"📁 repo: {run.repo_path}", 0),
            (6, f"🔧 workflow: {run.workflow}", 0),
            (7, f"📄 summary: {run.summary_path}", 0),
            (8, f"🕒 updated_at: {run.updated_at}", curses.A_DIM),
        )
        for row, line, attr in metadata:
            self._add(screen, row, 0, line, width - 1, attr)
        steps_y = 10
        self._add(screen, steps_y, 0, "🧩 steps", width - 1, curses.A_UNDERLINE | curses.A_BOLD)
        for index, step in enumerate(run.steps):
            row = steps_y + 1 + index
            if row >= height - 1:
                break
            is_active = step.name == active_step_name
            marker = "▶" if is_active else status_emoji(step.status)
            line = f"{marker} {step.name}\t{status_label(step.status)}\tattempts={step.attempts}\tduration={format_duration(step.duration_seconds)}"
            attr = self._status_attr(step.status) | (curses.A_BOLD if is_active else 0)
            if is_active:
                attr |= curses.A_REVERSE
            self._add(screen, row, 0, line, width - 1, attr)
        self._add(screen, height - 1, 0, "Esc/q:一覧へ  r:更新  l:ログ", width - 1)

    def _draw_logs(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        item = self.selected_item
        self._add(screen, 0, 0, "ログ末尾", width - 1, curses.A_BOLD)
        if item is None or item.run is None:
            self._add(screen, 2, 0, "選択中のrunログはありません。Escで戻る。", width - 1)
            return
        step = current_step(item.run)
        if step is None:
            self._add(screen, 2, 0, "ログ対象のstepはありません。", width - 1)
            return
        self._add(screen, 2, 0, f"{item.run.run_id} / {step.name}", width - 1, curses.A_UNDERLINE)
        lines = []
        for label, path in (("stdout", step.stdout_path), ("stderr", step.stderr_path)):
            lines.append(f"--- {label}: {path or '(なし)'} ---")
            if path:
                lines.extend(tail_lines(Path(path), 12))
        for index, line in enumerate(lines[: max(0, height - 5)]):
            self._add(screen, 4 + index, 0, line, width - 1)
        self._add(screen, height - 1, 0, "Esc/q:一覧へ  r:更新", width - 1)

    def _draw_menu(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        menu_width = min(max(42, max(len(label) for _, label in MENU_ITEMS) + 8), max(1, width - 2))
        menu_height = min(len(MENU_ITEMS) + 2, max(1, height - 2))
        x = max(0, (width - menu_width) // 2)
        y = max(0, (height - menu_height) // 2)
        try:
            screen.addstr(y, x, "┌" + "─" * max(0, menu_width - 2) + "┐")
            for row in range(1, menu_height - 1):
                screen.addstr(y + row, x, "│" + " " * max(0, menu_width - 2) + "│")
            screen.addstr(y + menu_height - 1, x, "└" + "─" * max(0, menu_width - 2) + "┘")
        except curses.error:
            return
        for index, (_, label) in enumerate(MENU_ITEMS[: max(0, menu_height - 2)]):
            attr = curses.A_REVERSE if index == self.menu_index else 0
            self._add(screen, y + 1 + index, x + 2, f"{index + 1}. {label}", menu_width - 4, attr)

    def _draw_command_prompt(self, screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        self._add(screen, height - 2, 0, ":" + self.command_buffer, width - 1, curses.A_BOLD)

    def _handle_dashboard_input(self, key: int) -> bool:
        if key in (ord("q"), ord("Q")):
            return True
        if key in (curses.KEY_UP, ord("k")):
            self.selected_index = max(0, self.selected_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected_index = min(max(0, len(self.items) - 1), self.selected_index + 1)
        elif key in (10, 13, ord("d")):
            self.view = "detail"
        elif key == ord("l"):
            self.view = "logs"
        elif key == ord("m"):
            self.view = "menu"
            self.menu_index = 0
        elif key == ord(":"):
            self.view = "command"
            self.command_buffer = ""
        elif key == ord("r"):
            self.refresh()
            self.message = "更新しました。"
        return False

    def _handle_menu_input(self, key: int) -> bool:
        if key in (27, ord("m")):
            self.view = "dashboard"
        elif key in (curses.KEY_UP, ord("k")):
            self.menu_index = max(0, self.menu_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.menu_index = min(len(MENU_ITEMS) - 1, self.menu_index + 1)
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

    def _handle_detail_input(self, key: int) -> bool:
        if key in (27, ord("q"), ord("Q")):
            self.view = "dashboard"
        elif key == ord("r"):
            self.refresh()
        elif key == ord("l"):
            self.view = "logs"
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
            self.view = "detail"
        elif command.name == "logs":
            self.view = "logs"
        elif command.name == "help":
            self.message = COMMAND_HELP
        self.view = "dashboard" if command.name not in {"detail", "logs"} else self.view
        return False

    @property
    def items(self) -> tuple[PipelineItem, ...]:
        return pipeline_items(self.snapshot, self.filter_name)

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

    def _item_line(self, item: PipelineItem) -> str:
        identifier = item.item_id[-20:]
        kind = "job" if item.kind == "job" else "run"
        current = ""
        if item.run and item.run.current_step:
            current = f" / {STEP_LABELS.get(item.run.current_step, item.run.current_step)}"
        return f"{status_symbol(item.status)} {kind} {identifier} {status_label(item.status)}{current}"

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


def active_step(run: PipelineRun) -> str | None:
    for step in run.steps:
        if step.status in ATTENTION_STATUSES or step.status == "running":
            return step.name
    return None


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


def format_duration(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"
