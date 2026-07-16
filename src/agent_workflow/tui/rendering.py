"""TUIのcurses描画。"""

from __future__ import annotations

import curses
from typing import TYPE_CHECKING

from agent_workflow.pipeline import (
    ATTENTION_STATUSES,
    PIPELINE_FILTERS,
    PipelineAttempt,
    PipelineItem,
    PipelineStep,
    pipeline_items,
)
from agent_workflow.tui_components import (
    ArtifactState,
    AttemptsState,
    DashboardState,
    LogsState,
    RunDetailState,
)

from .constants import FILTER_LABELS, MENU_ITEMS, STATUS_COLOR_PAIRS, STEP_LABELS
from .content import (
    artifact_label,
    compact_timestamp,
    current_step,
    format_duration,
    status_emoji,
    status_label,
    status_symbol,
)

if TYPE_CHECKING:
    from .app import TuiApp


class TuiRenderer:
    """TuiAppのread modelとcursorを使って画面だけを描画する。"""

    def __init__(self, app: TuiApp) -> None:
        self.app = app

    def draw(self, screen: curses.window) -> None:
        app = self.app
        screen.erase()
        screen_drawers = {
            "dashboard": self._draw_dashboard,
            "detail": self._draw_detail,
            "attempts": self._draw_attempts,
            "logs": self._draw_logs,
            "artifact": self._draw_artifact,
        }
        base_view = {
            DashboardState: "dashboard",
            RunDetailState: "detail",
            AttemptsState: "attempts",
            LogsState: "logs",
            ArtifactState: "artifact",
        }[type(app._screen_state)]
        base_drawer = screen_drawers[base_view]
        if app.view in {"menu", "command", "job"}:
            base_drawer = screen_drawers["detail"] if app.detail is not None else screen_drawers["dashboard"]
        base_drawer(screen)
        overlay_drawers = {
            "menu": self._draw_menu,
            "command": self._draw_command_prompt,
            "job": self._draw_job_detail,
        }
        overlay_drawer = overlay_drawers.get(app.view)
        if overlay_drawer is not None:
            overlay_drawer(screen)
        screen.refresh()

    def _draw_dashboard(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        items = app.items
        all_items = pipeline_items(app.snapshot, "all", include_jobs=False)
        title = f"aw runs  |  最終更新: {compact_timestamp(app.snapshot.generated_at)}"
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
        app._ensure_selection_visible(list_capacity)
        list_label = "run履歴"
        if len(items) > list_capacity:
            first = app.list_offset + 1
            last = min(len(items), app.list_offset + list_capacity)
            list_label += f" ({first}-{last}/{len(items)})"
        self._add(screen, 3, 0, list_label, width - 1, curses.A_UNDERLINE)
        self._add(screen, 4, 0, "  開始日時              経過       run                  状態       step", width - 1, curses.A_DIM)
        visible_items = items[app.list_offset : app.list_offset + list_capacity]
        for row, item in enumerate(visible_items):
            absolute_index = app.list_offset + row
            attr = curses.A_REVERSE if absolute_index == app.selected_index else 0
            self._draw_item(screen, list_top + row, item, width - 1, attr)
        if not items:
            self._add(screen, list_top, 0, "表示対象のrunはありません", width - 1)

        self._add(screen, height - 2, 0, app.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:選択  Enter/l:詳細  f:filter  m:menu  ::コマンド  q:終了", width - 1)

    def _draw_filter_tabs(self, screen: curses.window, x: int, y: int, width: int) -> None:
        app = self.app
        cursor = x
        for filter_name in PIPELINE_FILTERS:
            label = f" {FILTER_LABELS[filter_name]} "
            remaining = width - (cursor - x)
            if remaining <= 0:
                break
            attr = curses.A_REVERSE if filter_name == app.filter_name else curses.A_DIM
            self._add(screen, y, cursor, label, remaining, attr)
            cursor += len(label) + 1

    def _draw_detail(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        detail = app.detail
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
        if app.detail_focus == "steps":
            left_attr |= curses.A_REVERSE
        else:
            right_attr |= curses.A_REVERSE
        self._add(screen, section_y, 0, "🧩 Pipeline / step", left_width - 1, left_attr)
        self._add(screen, section_y, right_x, "📝 選択stepのログ", right_width, right_attr)
        step_capacity = max(1, height - section_y - 3)
        for index, step in enumerate(detail.steps[:step_capacity]):
            row = section_y + 1 + index
            selected = index == app.detail_step_index
            self._draw_step_row(screen, row, 0, step, left_width - 1, selected)
        self._draw_detail_logs(screen, right_x, section_y + 1, right_width, height - section_y - 3)
        self._add(screen, height - 2, 0, app.message, width - 1, curses.A_DIM)
        footer = (
            "↑↓/jk:step  l:ログへ  h:一覧へ  Enter/a:試行  Esc:一覧"
            if app.detail_focus == "steps"
            else "↑↓/jk:スクロール  g/G:先頭/末尾  h:stepへ  Tab/o/e:stdout/stderr  r:更新  Esc:一覧"
        )
        self._add(screen, height - 1, 0, footer, width - 1)

    def _draw_detail_logs(self, screen: curses.window, x: int, y: int, width: int, height: int) -> None:
        app = self.app
        step = app.selected_detail_step
        if step is None:
            self._add(screen, y, x, "ログ対象のstepはありません。", width)
            return
        attempt = app.selected_attempt
        attempt_label = f"#{attempt.attempt}" if attempt else "current"
        self._add(
            screen,
            y,
            x,
            f"{status_emoji(step.status)} {STEP_LABELS.get(step.name, step.name)} / {attempt_label} / {app.log_source}"
            f"  {'📡末尾追従' if app.log_follow else '📍履歴位置'}",
            width,
            self._status_attr(step.status) | curses.A_BOLD,
        )
        self._add(screen, y + 1, x, f"path: {app.selected_log_path or '(なし)'}", width, curses.A_DIM)
        visible = max(1, height - 2)
        max_offset = max(0, len(app.content_lines) - visible)
        if app.log_follow:
            app.content_offset = max_offset
        else:
            app.content_offset = min(max(0, app.content_offset), max_offset)
        if not app.content_lines:
            self._add(screen, y + 2, x, "(ログはありません)", width)
            return
        for index, line in enumerate(app.content_lines[app.content_offset : app.content_offset + visible]):
            self._add(screen, y + 2 + index, x, line, width)

    def _draw_attempts(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        detail = app.detail
        self._add(screen, 0, 0, "🧾 step試行履歴", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        step = app.selected_detail_step
        if step is None:
            self._add(screen, 2, 0, "選択中のstepはありません。Escで戻る。", width - 1)
            return
        attempts = app.selected_step_attempts
        self._add(screen, 1, 0, f"{detail.run_id} / {STEP_LABELS.get(step.name, step.name)}", width - 1, curses.A_UNDERLINE)
        if not attempts:
            self._add(screen, 3, 0, "このstepの試行履歴はありません。", width - 1)
        else:
            list_width = max(38, min(64, width // 2))
            self._add(screen, 3, 0, "試行", list_width - 1, curses.A_UNDERLINE)
            for index, attempt in enumerate(attempts[: max(1, height - 8)]):
                selected = index == app.detail_attempt_index
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
            self._draw_attempt_detail(screen, attempts[app.detail_attempt_index], right_x, 4, max(1, width - right_x - 1), height - 7)
        self._add(screen, height - 2, 0, app.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:試行  Enter/l:ログ  s:summary  Esc:runへ", width - 1)

    def _draw_logs(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        detail = app.detail
        self._add(screen, 0, 0, "📝 ログビューア", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        step = app.selected_detail_step
        if step is None:
            self._add(screen, 2, 0, "ログ対象のstepはありません。Escで戻る。", width - 1)
            return
        attempt = app.selected_attempt
        attempt_label = f"#{attempt.attempt}" if attempt else "current"
        path = app.selected_log_path
        self._add(
            screen,
            1,
            0,
            f"{detail.run_id} / {STEP_LABELS.get(step.name, step.name)} / {attempt_label} / {app.log_source}",
            width - 1,
            curses.A_UNDERLINE,
        )
        self._add(screen, 2, 0, f"path: {path or '(なし)'}", width - 1, curses.A_DIM)
        visible = max(1, height - 5)
        max_offset = max(0, len(app.content_lines) - visible)
        if app.log_follow:
            app.content_offset = max_offset
        else:
            app.content_offset = min(max(0, app.content_offset), max_offset)
        for index, line in enumerate(app.content_lines[app.content_offset : app.content_offset + visible]):
            self._add(screen, 3 + index, 0, line, width - 1)
        self._add(screen, height - 2, 0, app.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:スクロール  g/G:先頭/末尾  Tab/o/e:stdout/stderr  [/]:step  a:試行  r:更新  Esc", width - 1)

    def _draw_artifact(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        detail = app.detail
        self._add(screen, 0, 0, f"📄 {artifact_label(app.artifact_kind)}", width - 1, curses.A_BOLD)
        if detail is None:
            self._add(screen, 2, 0, "run詳細を読み込めません。Escで一覧へ戻る。", width - 1)
            return
        self._add(screen, 1, 0, f"{detail.run_id} / {app.artifact_path or '(なし)'}", width - 1, curses.A_UNDERLINE)
        visible = max(1, height - 4)
        max_offset = max(0, len(app.content_lines) - visible)
        app.content_offset = min(max(0, app.content_offset), max_offset)
        for index, line in enumerate(app.content_lines[app.content_offset : app.content_offset + visible]):
            self._add(screen, 3 + index, 0, line, width - 1)
        self._add(screen, height - 2, 0, app.message, width - 1, curses.A_DIM)
        self._add(screen, height - 1, 0, "↑↓/jk:スクロール  g/G:先頭/末尾  r:更新  Esc:runへ", width - 1)

    def _draw_job_detail(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        item = app.selected_item
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
        app = self.app
        height, width = screen.getmaxyx()
        app._screen_height = height
        menu_width = min(max(42, max(len(label) for _, label in MENU_ITEMS) + 8), max(1, width - 2))
        menu_height = min(len(MENU_ITEMS) + 2, max(1, height - 2))
        x = max(0, (width - menu_width) // 2)
        y = max(0, (height - menu_height) // 2)
        visible_count = max(0, menu_height - 2)
        app._ensure_menu_visible(visible_count)
        try:
            screen.addstr(y, x, "┌" + "─" * max(0, menu_width - 2) + "┐")
            for row in range(1, menu_height - 1):
                screen.addstr(y + row, x, "│" + " " * max(0, menu_width - 2) + "│")
            screen.addstr(y + menu_height - 1, x, "└" + "─" * max(0, menu_width - 2) + "┘")
        except curses.error:
            return
        for row, index in enumerate(range(app.menu_offset, min(len(MENU_ITEMS), app.menu_offset + visible_count))):
            _, label = MENU_ITEMS[index]
            attr = curses.A_REVERSE if index == app.menu_index else 0
            self._add(screen, y + 1 + row, x + 2, f"{index + 1}. {label}", menu_width - 4, attr)

    def _draw_command_prompt(self, screen: curses.window) -> None:
        app = self.app
        height, width = screen.getmaxyx()
        self._add(screen, height - 2, 0, ":" + app.command_buffer, width - 1, curses.A_BOLD)

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

    def init_colors(self) -> None:
        app = self.app
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
            app.colors_enabled = True
        except curses.error:
            app.colors_enabled = False

    def _status_attr(self, status: str) -> int:
        if not self.app.colors_enabled:
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
