"""workflow spanを設定済みOTLP collectorへ直接送信する。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from agent_workflow.telemetry import load_otlp_trace_runtime, normalize_telemetry_attributes


class TraceRecorder:
    """OTLPが設定されている場合だけrun/step spanを記録する。"""

    def __init__(
        self,
        run_attributes: dict[str, object] | None = None,
        otel_factory: Callable[[dict[str, object]], Any | None] | None = None,
    ) -> None:
        """optional OTLP sessionを初期化する。

        処理フロー:
        - [1] 呼び出し元からrun属性のsnapshotを分離する。
        - [2] trace export設定に応じてremote sessionを生成する。
        """

        # [1] 呼び出し元のdict変更が開始済みrun spanへ波及しないcopyを作る。
        attributes: dict[str, object] = dict(run_attributes or {})
        # [2] endpoint未設定・export無効時はno-op recorderとして動く。
        self._otel = (otel_factory or load_otlp_trace_runtime)(attributes)

    @contextmanager
    def span(self, name: str, **attrs: object) -> Iterator[dict[str, object]]:
        """1回のstep attemptをoptional OTLP spanとして記録する。

        処理フロー:
        - [1] 属性を正規化し、設定済みならremote子spanを開始する。
        - [2] 呼び出し元が結果属性・statusを追記できるdataを渡す。
        - [3] 未処理例外をERROR statusへ変換する。
        - [4] 最終属性とstatusでremote子spanを終了する。
        """

        # [1] metricsとtraceで同じ属性名を使い、子spanを親runへ接続する。
        normalized_attrs = normalize_telemetry_attributes(attrs)
        remote_span = self._otel.start_step(name, normalized_attrs) if self._otel else None
        # [2] runnerがcommand結果を追記する可変payloadだけを保持する。
        data: dict[str, object] = {"attributes": normalized_attrs}
        status_code = "OK"
        status_message = ""
        try:
            yield data
        except Exception as exc:
            # [3] runnerが明示statusを設定しない例外経路もERRORとして送る。
            status_code = "ERROR"
            status_message = str(exc)
            raise
        finally:
            # [4] OTLP無効時も同じ制御フローを保ち、local trace fileは作らない。
            status_code = str(data.pop("status_code", status_code))
            status_message = str(data.pop("status_message", status_message))
            final_raw_attributes = data.get("attributes", {})
            final_attributes = normalize_telemetry_attributes(
                final_raw_attributes if isinstance(final_raw_attributes, dict) else {}
            )
            if self._otel and remote_span is not None:
                self._otel.finish_step(remote_span, status_code, status_message, final_attributes)

    def close(self, run_status: str) -> None:
        """remote sessionがある場合だけ親run spanをflushして閉じる。

        処理フロー:
        - [1] OTLP有効時だけ最終run statusを渡してsessionを閉じる。
        """

        # [1] provider lifecycleはtelemetry runtimeへ集約する。
        if self._otel:
            self._otel.close(run_status)


def trace_enabled_hint() -> str:
    """credentialを含めず現在のtrace export設定を説明する。"""

    if os.environ.get("OTEL_TRACES_EXPORTER", "otlp").lower() == "none":
        return "OTLP trace export disabled"
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if endpoint:
        return "OTLP trace endpoint configured"
    return "OTLP trace endpoint not configured"
