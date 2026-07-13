from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class TraceRecorder:
    """Append durable OTel-shaped step spans without requiring the OTel SDK."""

    def __init__(self, path: Path, trace_id: str | None = None) -> None:
        self.path = path
        self.trace_id = trace_id or secrets.token_hex(16)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def span(self, name: str, **attrs: object) -> Iterator[dict[str, object]]:
        """Record one step attempt even when the wrapped operation raises."""

        span_id = secrets.token_hex(8)
        start_ns = time.time_ns()
        data: dict[str, object] = {"span_id": span_id, "attributes": attrs}
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
            attrs.update(data.get("attributes", {}))
            record = {
                "format": "otel-jsonl-v0",
                "trace_id": self.trace_id,
                "span_id": span_id,
                "parent_span_id": "",
                "name": name,
                "kind": "INTERNAL",
                "start_time_unix_nano": start_ns,
                "end_time_unix_nano": end_ns,
                "duration_ms": round((end_ns - start_ns) / 1_000_000, 3),
                "status": {"code": status_code, "message": status_message},
                "attributes": attrs,
            }
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")


def trace_enabled_hint() -> str:
    """Describe configured telemetry without persisting endpoint credentials."""

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if endpoint:
        return "OTLP endpoint configured; local trace.jsonl is always written"
    return "local trace.jsonl is always written"
