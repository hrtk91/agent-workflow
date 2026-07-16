"""TUIで共有するラベル、記号、表示上限。"""

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
