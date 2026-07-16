"""TUIのコマンドパレット入力。"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from agent_workflow.pipeline import PIPELINE_FILTERS


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
