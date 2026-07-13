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
        """集計snapshotをGaugeとして登録し、1回送信してproviderを閉じる。

        処理フロー:
        - [1] analytics用meterを取得する。
        - [2] 値が存在するreport fieldごとにObservableGaugeを登録する。
        - [3] 短命CLIの終了前に明示的に収集・送信する。
        - [4] 成否にかかわらずproviderをshutdownする。
        """

        instruments: list[Any] = []
        observation_count = 0
        try:
            # [1] report snapshot専用のinstrumentation scopeを使う。
            meter = self.provider.get_meter("agent-workflow.analytics", "0.1.0")
            # [2] nullだけのfieldはinstrumentを作らず、実値のあるGaugeだけ登録する。
            for spec in GAUGES:
                observations = self._observations(report, spec.field)
                if not observations:
                    continue
                observation_count += len(observations)

                # callback実行時に別fieldの値へ入れ替わらないよう、この時点のtupleを固定する。
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
            # [3] periodic collectionを待たず、今回登録したsnapshotを同期的に送る。
            if instruments and self.provider.force_flush(timeout_millis=10_000) is False:
                raise RuntimeError("OpenTelemetry metric force_flush failed")
            return observation_count
        finally:
            # [4] exporter threadやnetwork resourceを短命processに残さない。
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
        """run親spanと、子span生成に使うcontextを初期化する。

        処理フロー:
        - [1] provider・tracer・trace APIをsessionへ保持する。
        - [2] 正規化したrun属性で親spanを開始する。
        - [3] 後続step spanが同じtraceへ連結されるparent contextを作る。
        """

        # [1] lifecycleをこのsessionだけで完結できるよう依存objectを保持する。
        self.provider = provider
        self.tracer = tracer
        self.trace_api = trace_api
        self.closed = False
        # [2] workflow呼び出し全体を表す親spanを1つだけ開始する。
        self.root_span = tracer.start_span(
            "agent_workflow.run",
            attributes=normalize_telemetry_attributes(run_attributes),
        )
        # [3] 各step attemptを親spanへ接続するcontextを固定する。
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
        """step attemptの最終属性・statusを反映して子spanを終了する。

        処理フロー:
        - [1] command結果を含む最終属性を反映する。
        - [2] local JSONLと同じstatusを設定する。
        - [3] 子spanを終了してexport待ちqueueへ渡す。
        """

        # [1] span開始後に判明したexit codeやtimeoutも含めて上書きする。
        span.set_attributes(normalize_telemetry_attributes(attributes))
        # [2] local recordとremote spanの成否を同じ判定に揃える。
        span.set_status(self._status(status_code, status_message))
        # [3] BatchSpanProcessorが送信できる完了spanにする。
        span.end()

    def close(self, run_status: str) -> None:
        """run親spanを終了し、未送信spanをflushしてproviderを閉じる。

        処理フロー:
        - [1] 重複closeを抑止する。
        - [2] 最終run statusを親spanへ反映して終了する。
        - [3] queue済みspanを明示的に送信する。
        - [4] flush失敗時もproviderをshutdownする。
        """

        # [1] finally経路が重なってもspan終了とshutdownを1回に限定する。
        if self.closed:
            return
        self.closed = True
        try:
            # [2] workflowのterminal statusを親spanの属性とOTel statusへ反映する。
            self.root_span.set_attribute(ATTRIBUTE_NAMES["status"], run_status)
            root_status = "OK" if run_status == "succeeded" else "ERROR"
            self.root_span.set_status(self._status(root_status, "" if root_status == "OK" else run_status))
            self.root_span.end()
            # [3] CLI process終了前に全step spanと親spanをcollectorへ渡す。
            if self.provider.force_flush(timeout_millis=10_000) is False:
                raise RuntimeError("OpenTelemetry trace force_flush failed")
        finally:
            # [4] exporterのbackground resourceを必ず解放する。
            self.provider.shutdown()

    def _status(self, status_code: str, status_message: str) -> Any:
        code = self.trace_api.StatusCode.ERROR if status_code == "ERROR" else self.trace_api.StatusCode.OK
        return self.trace_api.Status(code, status_message or None)


def export_report_to_otel(
    report: dict[str, Any],
    runtime_factory: Callable[[], tuple[Any, Callable[..., Any]]] | None = None,
) -> int:
    """aw reportの集計結果がある場合だけOTLP/HTTPへ送信する。

    処理フロー:
    - [1] 空reportならOTel runtimeを初期化せず終了する。
    - [2] optional dependencyからmetric providerを構築する。
    - [3] report snapshotのGauge登録・送信をexporterへ委譲する。
    """

    # [1] 送信値がない場合はendpointやoptional dependencyを要求しない。
    if not report.get("rows"):
        return 0
    # [2] testではfactoryを注入し、本番ではOTLP runtimeを遅延生成する。
    provider, observation_type = (runtime_factory or load_otlp_runtime)()
    # [3] provider lifecycleを含む1回のsnapshot exportを実行する。
    return OtelReportExporter(provider, observation_type).export(report)


def load_otlp_runtime() -> tuple[Any, Callable[..., Any]]:
    """metric用の短命OTLP/HTTP runtimeを遅延構築する。

    処理フロー:
    - [1] endpoint・protocol・exporter有効状態を検証する。
    - [2] optional OTel packageを必要時だけimportする。
    - [3] periodic送信を無効化したreaderと共通Resourceを作る。
    - [4] 明示flush/shutdown前提のMeterProviderを返す。
    """

    # [1] signal固有設定を優先し、未設定・未対応protocolを早期に通知する。
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("set OTEL_EXPORTER_OTLP_METRICS_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT")
    protocol = (os.environ.get("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf").lower()
    if protocol not in {"http", "http/protobuf"}:
        raise RuntimeError("agent-workflow currently supports OTLP metrics over HTTP/protobuf")
    if os.environ.get("OTEL_METRICS_EXPORTER", "otlp").lower() == "none":
        raise RuntimeError("OTEL_METRICS_EXPORTER=none disables metric export")

    # [2] OTelを使わない通常CLIではbase dependencyを増やさない。
    try:
        metrics_api = import_module("opentelemetry.metrics")
        resource_module = import_module("opentelemetry.sdk.resources")
        sdk_metrics = import_module("opentelemetry.sdk.metrics")
        sdk_export = import_module("opentelemetry.sdk.metrics.export")
        otlp_export = import_module("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    except ModuleNotFoundError as exc:
        raise RuntimeError("install the OpenTelemetry extra: pip install 'agent-workflow[otel]'") from exc

    # [3] report commandは短命なためperiodic collectionを待たず、明示flushへ委ねる。
    exporter = otlp_export.OTLPMetricExporter()
    reader = sdk_export.PeriodicExportingMetricReader(exporter, export_interval_millis=math.inf)
    resource = build_otel_resource(resource_module)
    # [4] atexit任せにせずOtelReportExporterがlifecycleを閉じるproviderを返す。
    provider = sdk_metrics.MeterProvider(
        resource=resource,
        metric_readers=[reader],
        shutdown_on_exit=False,
    )
    return provider, metrics_api.Observation


def load_otlp_trace_runtime(run_attributes: dict[str, object]) -> OtelTraceSession | None:
    """trace export設定時だけrun単位のOTLP/HTTP sessionを構築する。

    処理フロー:
    - [1] trace export無効化とendpoint未設定を判定する。
    - [2] signal固有protocolがHTTP/protobufであることを検証する。
    - [3] optional OTel packageを必要時だけimportする。
    - [4] run専用provider・processor・tracerを組み立ててsessionを返す。
    """

    # [1] 明示無効化またはendpoint未設定ならlocal JSONLだけを使用する。
    if os.environ.get("OTEL_TRACES_EXPORTER", "otlp").lower() == "none":
        return None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    # [2] 現在実装しているHTTP/protobuf以外を暗黙に誤送信しない。
    protocol = (os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf").lower()
    if protocol not in {"http", "http/protobuf"}:
        raise RuntimeError("agent-workflow currently supports OTLP traces over HTTP/protobuf")

    # [3] endpointを指定した利用者にだけotel extraを要求する。
    try:
        trace_api = import_module("opentelemetry.trace")
        resource_module = import_module("opentelemetry.sdk.resources")
        sdk_trace = import_module("opentelemetry.sdk.trace")
        sdk_trace_export = import_module("opentelemetry.sdk.trace.export")
        otlp_trace_export = import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    except ModuleNotFoundError as exc:
        raise RuntimeError("install the OpenTelemetry extra: pip install 'agent-workflow[otel]'") from exc

    # [4] invocationごとにproviderを分け、resume/retryの新attemptだけを確実にflushする。
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
