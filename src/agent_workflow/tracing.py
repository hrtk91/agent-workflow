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
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._otel = (otel_factory or load_otlp_trace_runtime)(run_attributes or {})
        self.trace_id = self._otel.trace_id if self._otel else trace_id or secrets.token_hex(16)
        self.parent_span_id = self._otel.root_span_id if self._otel else ""

    @contextmanager
    def span(self, name: str, **attrs: object) -> Iterator[dict[str, object]]:
        """Record one step attempt even when the wrapped operation raises."""

        normalized_attrs = normalize_telemetry_attributes(attrs)
        remote_span = self._otel.start_step(name, normalized_attrs) if self._otel else None
        span_id = (
            format(remote_span.get_span_context().span_id, "016x")
            if remote_span is not None
            else secrets.token_hex(8)
        )
        start_ns = time.time_ns()
        data: dict[str, object] = {"span_id": span_id, "attributes": normalized_attrs}
        status_code = "OK"
        status_message = ""
        try:
            yield data
        except Exception as exc:
            status_code = "ERROR"
            status_message = str(exc)
            raise
        finally:
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
                if self._otel and remote_span is not None:
                    self._otel.finish_step(remote_span, status_code, status_message, final_attributes)

    def close(self, run_status: str) -> None:
        """Flush and close the optional remote run trace."""

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
