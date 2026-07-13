"""Optional OTLP/HTTP runtimes for workflow traces and analytics reports."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable


ATTRIBUTE_NAMES = {
    "model": "gen_ai.request.model",
    "provider": "gen_ai.provider.name",
    "task_type": "agent_workflow.task.type",
    "workflow": "agent_workflow.workflow.name",
    "repo": "agent_workflow.repository",
    "repo_path": "agent_workflow.repository.path",
    "status": "agent_workflow.run.status",
    "run_id": "agent_workflow.run.id",
    "purpose": "agent_workflow.run.purpose",
    "executor_bin": "agent_workflow.executor.name",
    "attempt": "agent_workflow.step.attempt",
    "exit_code": "process.exit.code",
    "timed_out": "agent_workflow.step.timed_out",
    "error": "error.message",
    "stdout_path": "agent_workflow.log.stdout.path",
    "stderr_path": "agent_workflow.log.stderr.path",
}


@dataclass(frozen=True)
class GaugeSpec:
    name: str
    field: str
    unit: str
    description: str


GAUGES = (
    GaugeSpec("agent_workflow.analytics.runs", "runs", "{run}", "Completed workflow runs in the report group"),
    GaugeSpec("agent_workflow.analytics.qc.runs", "qc_runs", "{run}", "Report-group runs that executed QC"),
    GaugeSpec("agent_workflow.analytics.qc.first_pass.rate", "first_pass_qc_rate", "%", "First-pass QC success rate"),
    GaugeSpec("agent_workflow.analytics.qc.eventual.rate", "eventual_qc_rate", "%", "Eventual QC success rate"),
    GaugeSpec("agent_workflow.analytics.qc.attempts.p50", "qc_attempts_p50", "{attempt}", "Median QC attempts per run"),
    GaugeSpec("agent_workflow.analytics.run.duration.p50", "elapsed_seconds_p50", "s", "Median workflow run duration"),
    GaugeSpec("agent_workflow.analytics.changed.lines.p50", "changed_lines_p50", "{line}", "Median changed lines per run"),
)


class OtelReportExporter:
    """Publish one grouped report snapshot through an injected OTel provider."""

    def __init__(self, provider: Any, observation_type: Callable[..., Any]) -> None:
        self.provider = provider
        self.observation_type = observation_type

    def export(self, report: dict[str, Any]) -> int:
        """Register snapshot gauges, force a collection, then close the provider."""

        instruments: list[Any] = []
        observation_count = 0
        try:
            meter = self.provider.get_meter("agent-workflow.analytics", "0.1.0")
            for spec in GAUGES:
                observations = self._observations(report, spec.field)
                if not observations:
                    continue
                observation_count += len(observations)

                # Bind the immutable tuple at definition time; OTel invokes the
                # callback later during the explicit force_flush below.
                def callback(_options: object, values: tuple[Any, ...] = tuple(observations)) -> tuple[Any, ...]:
                    return values

                instruments.append(
                    meter.create_observable_gauge(
                        spec.name,
                        callbacks=[callback],
                        unit=spec.unit,
                        description=spec.description,
                    )
                )
            if instruments and self.provider.force_flush(timeout_millis=10_000) is False:
                raise RuntimeError("OpenTelemetry metric force_flush failed")
            return observation_count
        finally:
            self.provider.shutdown(timeout_millis=10_000)

    def _observations(self, report: dict[str, Any], field: str) -> list[Any]:
        """Convert non-null report values into low-cardinality OTel observations."""

        observations: list[Any] = []
        for row in report.get("rows") or []:
            value = row.get(field)
            if value is None:
                continue
            attributes = {
                ATTRIBUTE_NAMES.get(str(key), f"agent_workflow.group.{key}"): str(group_value)
                for key, group_value in (row.get("group") or {}).items()
            }
            observations.append(self.observation_type(value, attributes=attributes))
        return observations


class OtelTraceSession:
    """Own one remote run span and its child step-attempt spans."""

    def __init__(self, provider: Any, tracer: Any, trace_api: Any, run_attributes: dict[str, object]) -> None:
        self.provider = provider
        self.tracer = tracer
        self.trace_api = trace_api
        self.closed = False
        self.root_span = tracer.start_span(
            "agent_workflow.run",
            attributes=normalize_telemetry_attributes(run_attributes),
        )
        self.root_context = trace_api.set_span_in_context(self.root_span)

    @property
    def trace_id(self) -> str:
        """Return the remote root trace ID in the JSONL-compatible format."""

        return format(self.root_span.get_span_context().trace_id, "032x")

    @property
    def root_span_id(self) -> str:
        """Return the remote root span ID in the JSONL-compatible format."""

        return format(self.root_span.get_span_context().span_id, "016x")

    def start_step(self, name: str, attributes: Mapping[str, object]) -> Any:
        """Start a child span beneath the run span."""

        return self.tracer.start_span(
            name,
            context=self.root_context,
            attributes=normalize_telemetry_attributes(attributes),
        )

    def finish_step(
        self,
        span: Any,
        status_code: str,
        status_message: str,
        attributes: Mapping[str, object],
    ) -> None:
        """Apply final attempt attributes and end the remote child span."""

        span.set_attributes(normalize_telemetry_attributes(attributes))
        span.set_status(self._status(status_code, status_message))
        span.end()

    def close(self, run_status: str) -> None:
        """End the run span, flush queued spans, and close the provider once."""

        if self.closed:
            return
        self.closed = True
        try:
            self.root_span.set_attribute(ATTRIBUTE_NAMES["status"], run_status)
            root_status = "OK" if run_status == "succeeded" else "ERROR"
            self.root_span.set_status(self._status(root_status, "" if root_status == "OK" else run_status))
            self.root_span.end()
            if self.provider.force_flush(timeout_millis=10_000) is False:
                raise RuntimeError("OpenTelemetry trace force_flush failed")
        finally:
            self.provider.shutdown()

    def _status(self, status_code: str, status_message: str) -> Any:
        code = self.trace_api.StatusCode.ERROR if status_code == "ERROR" else self.trace_api.StatusCode.OK
        return self.trace_api.Status(code, status_message or None)


def export_report_to_otel(
    report: dict[str, Any],
    runtime_factory: Callable[[], tuple[Any, Callable[..., Any]]] | None = None,
) -> int:
    """Export an `aw report` snapshot through OTLP/HTTP when rows exist."""

    if not report.get("rows"):
        return 0
    provider, observation_type = (runtime_factory or load_otlp_runtime)()
    return OtelReportExporter(provider, observation_type).export(report)


def load_otlp_runtime() -> tuple[Any, Callable[..., Any]]:
    """Load optional OTel packages and build a short-lived OTLP/HTTP provider."""

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("set OTEL_EXPORTER_OTLP_METRICS_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT")
    protocol = (os.environ.get("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf").lower()
    if protocol not in {"http", "http/protobuf"}:
        raise RuntimeError("agent-workflow currently supports OTLP metrics over HTTP/protobuf")
    if os.environ.get("OTEL_METRICS_EXPORTER", "otlp").lower() == "none":
        raise RuntimeError("OTEL_METRICS_EXPORTER=none disables metric export")

    try:
        metrics_api = import_module("opentelemetry.metrics")
        resource_module = import_module("opentelemetry.sdk.resources")
        sdk_metrics = import_module("opentelemetry.sdk.metrics")
        sdk_export = import_module("opentelemetry.sdk.metrics.export")
        otlp_export = import_module("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    except ModuleNotFoundError as exc:
        raise RuntimeError("install the OpenTelemetry extra: pip install 'agent-workflow[otel]'") from exc

    # The report command is short lived, so disable periodic collection and
    # rely on OtelReportExporter.force_flush before shutdown.
    exporter = otlp_export.OTLPMetricExporter()
    reader = sdk_export.PeriodicExportingMetricReader(exporter, export_interval_millis=math.inf)
    resource = build_otel_resource(resource_module)
    provider = sdk_metrics.MeterProvider(
        resource=resource,
        metric_readers=[reader],
        shutdown_on_exit=False,
    )
    return provider, metrics_api.Observation


def load_otlp_trace_runtime(run_attributes: dict[str, object]) -> OtelTraceSession | None:
    """Build a per-run OTLP/HTTP trace session when trace export is configured."""

    if os.environ.get("OTEL_TRACES_EXPORTER", "otlp").lower() == "none":
        return None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    protocol = (os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf").lower()
    if protocol not in {"http", "http/protobuf"}:
        raise RuntimeError("agent-workflow currently supports OTLP traces over HTTP/protobuf")

    try:
        trace_api = import_module("opentelemetry.trace")
        resource_module = import_module("opentelemetry.sdk.resources")
        sdk_trace = import_module("opentelemetry.sdk.trace")
        sdk_trace_export = import_module("opentelemetry.sdk.trace.export")
        otlp_trace_export = import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    except ModuleNotFoundError as exc:
        raise RuntimeError("install the OpenTelemetry extra: pip install 'agent-workflow[otel]'") from exc

    # One provider is scoped to one workflow invocation so resume/retry runs
    # can flush every new attempt before the short-lived CLI process exits.
    provider = sdk_trace.TracerProvider(
        resource=build_otel_resource(resource_module),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(
        sdk_trace_export.BatchSpanProcessor(otlp_trace_export.OTLPSpanExporter())
    )
    tracer = provider.get_tracer("agent-workflow.workflow", "0.1.0")
    return OtelTraceSession(provider, tracer, trace_api, run_attributes)


def normalize_telemetry_attributes(attributes: Mapping[str, object]) -> dict[str, str | bool | int | float]:
    """Map local field names to stable OTel names and scalar values."""

    normalized: dict[str, str | bool | int | float] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        raw_name = str(key)
        name = raw_name if "." in raw_name else ATTRIBUTE_NAMES.get(raw_name, f"agent_workflow.{raw_name.replace('_', '.')}")
        if isinstance(value, (str, bool, int, float)):
            normalized[name] = value
        else:
            normalized[name] = str(value)
    return normalized


def build_otel_resource(resource_module: Any) -> Any:
    """Create the Resource shared by the trace and metric providers."""

    return resource_module.Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "agent-workflow"),
            "service.version": "0.1.0",
        }
    )
