"""task packet入力量とGit変更量を計測する。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from agent_workflow.analytics.constants import TASK_PACKET_NAMES
from agent_workflow.state import RunState


def task_packet_identity(task_dir: Path) -> tuple[str | None, int | None]:
    """順序固定のtask packetから再現可能なhashとbyte数を計算する。

    処理フロー:
    - [1] 対象ファイルを定義順に読み、ファイル名と内容をhashへ加える。
    - [2] 1ファイル以上読めた場合だけ識別子を返す。
    """

    # [1] 同じ内容でもfile境界が異なるpacketを別入力として扱う。
    digest = hashlib.sha256()
    total = 0
    found = False
    for name in TASK_PACKET_NAMES:
        path = task_dir / name
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        found = True
        total += len(data)
        digest.update(name.encode("utf-8") + b"\0" + data + b"\0")
    # [2] task生成前など入力が存在しない状態をnullで表す。
    return (digest.hexdigest(), total) if found else (None, None)


def collect_change_stats(state: RunState) -> tuple[int, int, int] | None:
    """run開始refに対するtracked/untracked変更量を集計する。

    処理フロー:
    - [1] 比較可能なworktreeとbase refがあることを確認する。
    - [2] git diff --numstatからtracked fileの増減を集計する。
    - [3] untracked text fileを追加行として重複なく集計する。
    - [4] file数・追加行・削除行を返し、Git失敗時は未計測とする。
    """

    # [1] worktree未作成・cleanup済み・base未確定のrunは計測できない。
    if not state.worktree_path or not state.base_ref:
        return None
    worktree = Path(state.worktree_path)
    if not worktree.is_dir():
        return None
    try:
        # [2] binaryと内部TAKT成果物を除き、tracked差分のfile数と行数を数える。
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--numstat", state.base_ref],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if diff.returncode != 0:
            return None
        seen: set[str] = set()
        additions = 0
        deletions = 0
        for line in diff.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            if excluded_metric_path(path):
                continue
            seen.add(path)
            if added.isdigit():
                additions += int(added)
            if deleted.isdigit():
                deletions += int(deleted)

        # [3] tracked側で数えたpathを除外し、text fileだけを追加行として扱う。
        untracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if untracked.returncode != 0:
            return None
        for raw_path in untracked.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="replace")
            if path in seen or excluded_metric_path(path):
                continue
            seen.add(path)
            try:
                data = (worktree / path).read_bytes()
            except OSError:
                continue
            if b"\0" not in data:
                additions += len(data.splitlines())
        # [4] 途中のGit/filesystem失敗では不正確な0を保存せずnull扱いにする。
        return len(seen), additions, deletions
    except OSError:
        return None


def excluded_metric_path(path: str) -> bool:
    return path == ".takt/runs" or path.startswith(".takt/runs/")
