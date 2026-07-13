"""SQLiteのrun factsを集計し、CLI表示用へ整形する。"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from agent_workflow.analytics.constants import GROUP_FIELDS, TERMINAL_RUN_STATUSES


def build_report(
    conn: sqlite3.Connection,
    group_by: Iterable[str],
    repo_path: str | None = None,
    since: str | None = None,
    include_repair: bool = False,
) -> dict[str, Any]:
    """完了runを指定軸で集計し、QC通過率と中央値を返す。

    処理フロー:
    - [1] group-by指定を検証する。
    - [2] terminal run・purpose・repository・期間の検索条件を組み立てる。
    - [3] SQLiteから対象runを取得する。
    - [4] 指定されたdimension値でrunをグルーピングする。
    - [5] QC実行runだけを分母に通過率と各中央値を算出する。
    - [6] 生成時刻・条件・集計行を構造化して返す。
    """

    # [1] SQL列名へ変換できる既知dimensionだけを受け付ける。
    groups = tuple(group_by)
    invalid = [field for field in groups if field not in GROUP_FIELDS]
    if invalid:
        raise ValueError(f"unsupported report group: {', '.join(invalid)}")
    if not groups:
        raise ValueError("--group-by must contain at least one field")

    # [2] repair runを既定で除外し、指定された絞り込みだけをparameter化する。
    where = [f"status in ({','.join('?' for _ in TERMINAL_RUN_STATUSES)})"]
    params: list[object] = []
    params.extend(sorted(TERMINAL_RUN_STATUSES))
    if not include_repair:
        where.append("purpose = 'workflow'")
    if repo_path:
        where.append("repo_path = ?")
        params.append(repo_path)
    if since:
        where.append("created_at >= ?")
        params.append(since)

    # [3] report対象となる完了runを時系列順で取得する。
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"select * from run_metrics where {' and '.join(where)} order by created_at",
        params,
    ).fetchall()

    # [4] nullや空値を表示用の安定したdimension値へ正規化してまとめる。
    grouped: dict[tuple[str, ...], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = tuple(display_dimension(row[GROUP_FIELDS[field]], field) for field in groups)
        grouped[key].append(row)

    # [5] QC未実行runを通過率の分母から外し、比較用の統計値を作る。
    report_rows: list[dict[str, Any]] = []
    for key, members in sorted(grouped.items()):
        first_pass = [int(row["first_pass_qc"]) for row in members if row["first_pass_qc"] is not None]
        eventual = [int(row["eventual_qc"]) for row in members if row["eventual_qc"] is not None]
        attempts = [int(row["qc_attempts"]) for row in members if row["first_pass_qc"] is not None]
        elapsed = [float(row["elapsed_seconds"]) for row in members if row["elapsed_seconds"] is not None]
        changed = [
            int(row["additions"]) + int(row["deletions"])
            for row in members
            if row["additions"] is not None and row["deletions"] is not None
        ]
        first_successes = sum(first_pass)
        eventual_successes = sum(eventual)
        report_rows.append(
            {
                "group": dict(zip(groups, key, strict=True)),
                "runs": len(members),
                "qc_runs": len(first_pass),
                "first_pass_qc_rate": rate(first_successes, len(first_pass)),
                "first_pass_qc_ci95": wilson_interval(first_successes, len(first_pass)),
                "eventual_qc_rate": rate(eventual_successes, len(eventual)),
                "eventual_qc_ci95": wilson_interval(eventual_successes, len(eventual)),
                "qc_attempts_p50": rounded_median(attempts),
                "elapsed_seconds_p50": rounded_median(elapsed),
                "changed_lines_p50": rounded_median(changed),
            }
        )

    # [6] text表示とOTel exportが同じpayloadを利用できる形で返す。
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "group_by": list(groups),
        "filters": {
            "repo_path": repo_path,
            "since": since,
            "include_repair": include_repair,
        },
        "rows": report_rows,
    }


def render_text_report(report: dict[str, Any]) -> str:
    """構造化reportをterminal向けの固定幅tableへ変換する。

    処理フロー:
    - [1] 空結果を短いmessageとして返す。
    - [2] 各集計行を表示文字列へ変換し、列幅を算出する。
    - [3] header・区切り・data行を組み立てる。
    """

    # [1] headerだけのtableを出さず、絞り込み結果が空であることを明示する。
    rows = list(report["rows"])
    if not rows:
        return "No completed workflow runs matched."

    # [2] 数値とdurationを表示用へ整形し、内容が切れない最大列幅を求める。
    headers = [
        "group",
        "runs",
        "qc",
        "first-pass",
        "eventual",
        "attempts p50",
        "elapsed p50",
        "changed p50",
    ]
    values: list[list[str]] = []
    for row in rows:
        group = ",".join(f"{key}={value}" for key, value in row["group"].items())
        values.append(
            [
                group,
                str(row["runs"]),
                str(row["qc_runs"]),
                format_rate(row["first_pass_qc_rate"]),
                format_rate(row["eventual_qc_rate"]),
                format_number(row["qc_attempts_p50"]),
                format_duration(row["elapsed_seconds_p50"]),
                format_number(row["changed_lines_p50"]),
            ]
        )
    widths = [max(len(headers[index]), *(len(row[index]) for row in values)) for index in range(len(headers))]
    # [3] すべての行を同じ列幅で左寄せして出力する。
    lines = ["Agent workflow report", "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in values:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def display_dimension(value: object, field: str) -> str:
    if value is None or str(value) == "":
        return "(default)" if field in {"model", "provider"} else "unspecified"
    return str(value)


def rate(successes: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(successes / total * 100, 1)


def wilson_interval(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.96
    observed = successes / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(observed * (1 - observed) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, center - margin) * 100, 1), round(min(1.0, center + margin) * 100, 1)]


def rounded_median(values: list[int] | list[float]) -> float | None:
    if not values:
        return None
    return round(float(median(values)), 1)


def format_rate(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}%"


def format_number(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def format_duration(value: Any) -> str:
    if value is None:
        return "-"
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"
