"""Optional OTLP/HTTP export for SQLite-backed analytics reports."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable


ATTRIBUTE_NAMES = {
    "model": "gen_ai.request.model",
    "provider": "gen_ai.provider.name",
    "task_type": "agent_workflow.task.type",
    "workflow": "agent_workflow.workflow.name",
    "repo": "agent_workflow.repository",
    "status": "agent_workflow.run.status",
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

        meter = self.provider.get_meter("agent-workflow.analytics", "0.1.0")
        instruments: list[Any] = []
        observation_count = 0
        try:
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
    resource = resource_module.Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "agent-workflow")}
    )
    provider = sdk_metrics.MeterProvider(
        resource=resource,
        metric_readers=[reader],
        shutdown_on_exit=False,
    )
    return provider, metrics_api.Observation
