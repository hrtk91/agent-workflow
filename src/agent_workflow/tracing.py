from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from agent_workflow.telemetry import OtelTraceSession, load_otlp_trace_runtime, normalize_telemetry_attributes


class TraceRecorder:
    """Write durable JSONL spans and optionally mirror them to OTLP."""

    def __init__(
        self,
        path: Path,
        trace_id: str | None = None,
        run_attributes: dict[str, object] | None = None,
        otel_factory: Callable[[dict[str, object]], Any | None] | None = None,
    ) -> None:
        """local trace保存先とoptional OTLP sessionを初期化する。

        処理フロー:
        - [1] trace.jsonlの親directoryを準備する。
        - [2] trace export設定に応じてremote sessionを遅延生成する。
        - [3] local/remoteで共有するtrace IDと親span IDを確定する。
        """

        # [1] 最初のstep完了時に必ずappendできる保存先を作る。
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # [2] endpoint未設定時はSDKを読み込まず、local-only recorderとして動く。
        self._otel = (otel_factory or load_otlp_trace_runtime)(run_attributes or {})
        # [3] remote利用時はcollectorへ送るIDをJSONLにも使い、相互参照可能にする。
        self.trace_id = self._otel.trace_id if self._otel else trace_id or secrets.token_hex(16)
        self.parent_span_id = self._otel.root_span_id if self._otel else ""

    @contextmanager
    def span(self, name: str, **attrs: object) -> Iterator[dict[str, object]]:
        """1回のstep attemptをlocal JSONLとoptional OTLPへ二重記録する。

        処理フロー:
        - [1] 属性を正規化し、設定済みならremote子spanを開始する。
        - [2] local record用のID・開始時刻・可変dataを準備する。
        - [3] 呼び出し元処理を実行し、未処理例外をERROR statusへ変換する。
        - [4] 最終属性と終了時刻をJSONLへ必ず追記する。
        - [5] local書き込み結果にかかわらずremote子spanを終了する。
        """

        # [1] metricsとtraceで同じ属性名を使い、remote spanを親runへ接続する。
        normalized_attrs = normalize_telemetry_attributes(attrs)
        remote_span = self._otel.start_step(name, normalized_attrs) if self._otel else None
        # [2] remoteがない場合だけlocal IDを生成し、開始時刻と可変dataを準備する。
        span_id = (
            format(remote_span.get_span_context().span_id, "016x")
            if remote_span is not None
            else secrets.token_hex(8)
        )
        start_ns = time.time_ns()
        data: dict[str, object] = {"span_id": span_id, "attributes": normalized_attrs}
        status_code = "OK"
        status_message = ""
        # [3] runnerが明示statusを設定しない例外経路もERRORとして残す。
        try:
            yield data
        except Exception as exc:
            status_code = "ERROR"
            status_message = str(exc)
            raise
        finally:
            # [4] runnerが追加したcommand結果を反映し、1attemptにつき1行をappendする。
            end_ns = time.time_ns()
            status_code = str(data.pop("status_code", status_code))
            status_message = str(data.pop("status_message", status_message))
            final_raw_attributes = data.get("attributes", {})
            final_attributes = normalize_telemetry_attributes(
                final_raw_attributes if isinstance(final_raw_attributes, dict) else {}
            )
            record = {
                "format": "otel-jsonl-v0",
                "trace_id": self.trace_id,
                "span_id": span_id,
                "parent_span_id": self.parent_span_id,
                "name": name,
                "kind": "INTERNAL",
                "start_time_unix_nano": start_ns,
                "end_time_unix_nano": end_ns,
                "duration_ms": round((end_ns - start_ns) / 1_000_000, 3),
                "status": {"code": status_code, "message": status_message},
                "attributes": final_attributes,
            }
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            finally:
                # [5] local artifactの書き込み失敗時もremote spanを未終了にしない。
                if self._otel and remote_span is not None:
                    self._otel.finish_step(remote_span, status_code, status_message, final_attributes)

    def close(self, run_status: str) -> None:
        """remote sessionがある場合だけ親run spanをflushして閉じる。

        処理フロー:
        - [1] local-onlyでは何もせず、remote利用時だけ最終statusを渡してcloseする。
        """

        # [1] provider lifecycleはOtelTraceSessionへ集約する。
        if self._otel:
            self._otel.close(run_status)


def trace_enabled_hint() -> str:
    """Describe configured telemetry without persisting endpoint credentials."""

    if os.environ.get("OTEL_TRACES_EXPORTER", "otlp").lower() == "none":
        return "local trace.jsonl is always written"
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        return "OTLP endpoint configured; local trace.jsonl is always written"
    return "local trace.jsonl is always written"
