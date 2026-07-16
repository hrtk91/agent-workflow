"""TUIが表示するログ、artifact、状態ラベルの読み取り処理。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_workflow.pipeline import (
    ATTENTION_STATUSES,
    PipelineRun,
    PipelineRunDetail,
    PipelineStep,
)

from .constants import (
    MAX_ARTIFACT_BYTES,
    MAX_CONTENT_LINES,
    MAX_LOG_LINE_CHARS,
    MAX_LOG_TAIL_BYTES,
    STATUS_EMOJIS,
    STATUS_LABELS,
    STATUS_SYMBOLS,
)


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
    """ログビューア向けに、ファイル末尾をbounded readして行列へ変換する。"""

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
