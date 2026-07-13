from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.cli import build_parser
from agent_workflow.telemetry import export_report_to_otel, load_otlp_trace_runtime
from agent_workflow.tracing import TraceRecorder, trace_enabled_hint


@dataclass(frozen=True)
class FakeObservation:
    value: int | float
    attributes: dict[str, str]


class FakeMeter:
    def __init__(self) -> None:
        self.gauges: dict[str, dict[str, Any]] = {}

    def create_observable_gauge(
        self,
        name: str,
        *,
        callbacks: list[Callable[[object], tuple[FakeObservation, ...]]],
        unit: str,
        description: str,
    ) -> object:
        self.gauges[name] = {
            "callbacks": callbacks,
            "unit": unit,
            "description": description,
        }
        return object()


class FakeProvider:
    def __init__(self) -> None:
        self.meter = FakeMeter()
        self.observations: dict[str, tuple[FakeObservation, ...]] = {}
        self.flushed = False
        self.shutdown_called = False

    def get_meter(self, _name: str, _version: str) -> FakeMeter:
        return self.meter

    def force_flush(self, timeout_millis: int) -> bool:
        self.flushed = timeout_millis == 10_000
        for name, gauge in self.meter.gauges.items():
            self.observations[name] = gauge["callbacks"][0](object())
        return True

    def shutdown(self, timeout_millis: int) -> None:
        self.shutdown_called = timeout_millis == 10_000


@dataclass(frozen=True)
class FakeSpanContext:
    trace_id: int
    span_id: int


class FakeRemoteSpan:
    def __init__(self, span_id: int) -> None:
        self.context = FakeSpanContext(int("a" * 32, 16), span_id)

    def get_span_context(self) -> FakeSpanContext:
        return self.context


class FakeTraceSession:
    trace_id = "a" * 32
    root_span_id = "b" * 16

    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, object], FakeRemoteSpan]] = []
        self.finished: list[tuple[FakeRemoteSpan, str, str, dict[str, object]]] = []
        self.run_status = ""

    def start_step(self, name: str, attributes: dict[str, object]) -> FakeRemoteSpan:
        span = FakeRemoteSpan(len(self.started) + 1)
        self.started.append((name, attributes, span))
        return span

    def finish_step(
        self,
        span: FakeRemoteSpan,
        status_code: str,
        status_message: str,
        attributes: dict[str, object],
    ) -> None:
        self.finished.append((span, status_code, status_message, attributes))

    def close(self, run_status: str) -> None:
        self.run_status = run_status


class OtelReportExporterTest(unittest.TestCase):
    def test_exports_grouped_report_as_snapshot_gauges(self) -> None:
        report = {
            "rows": [
                {
                    "group": {"model": "gpt-test", "task_type": "bug_fix"},
                    "runs": 4,
                    "qc_runs": 4,
                    "first_pass_qc_rate": 75.0,
                    "eventual_qc_rate": 100.0,
                    "qc_attempts_p50": 1.5,
                    "elapsed_seconds_p50": 42.0,
                    "changed_lines_p50": 18.0,
                }
            ]
        }
        provider = FakeProvider()

        observation_count = export_report_to_otel(
            report,
            runtime_factory=lambda: (provider, FakeObservation),
        )

        self.assertEqual(7, observation_count)
        self.assertTrue(provider.flushed)
        self.assertTrue(provider.shutdown_called)
        first_pass = provider.observations["agent_workflow.analytics.qc.first_pass.rate"][0]
        self.assertEqual(75.0, first_pass.value)
        self.assertEqual(
            {"gen_ai.request.model": "gpt-test", "agent_workflow.task.type": "bug_fix"},
            first_pass.attributes,
        )

    def test_empty_report_does_not_initialize_otel_runtime(self) -> None:
        called = False

        def runtime_factory() -> tuple[Any, Callable[..., Any]]:
            nonlocal called
            called = True
            raise AssertionError("runtime must not be loaded")

        self.assertEqual(0, export_report_to_otel({"rows": []}, runtime_factory=runtime_factory))
        self.assertFalse(called)

    def test_report_parser_accepts_otel_export_flag(self) -> None:
        args = build_parser().parse_args(["report", "--export-otel"])

        self.assertTrue(args.export_otel)

    def test_trace_hint_does_not_persist_endpoint_value(self) -> None:
        endpoint = "https://collector.example.invalid?token=secret"
        with mock.patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}, clear=True):
            hint = trace_enabled_hint()

        self.assertIn("configured", hint)
        self.assertNotIn(endpoint, hint)

    def test_trace_recorder_mirrors_step_to_jsonl_and_remote_session(self) -> None:
        session = FakeTraceSession()
        captured_run_attributes: dict[str, object] = {}

        def factory(attributes: dict[str, object]) -> FakeTraceSession:
            captured_run_attributes.update(attributes)
            return session

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            recorder = TraceRecorder(
                trace_path,
                run_attributes={"run_id": "run-1", "model": "gpt-test"},
                otel_factory=factory,
            )
            with recorder.span("agent_workflow.step.run_qc", attempt=2, task_type="bug_fix") as span:
                span_attributes = cast(dict[str, object], span["attributes"])
                span["attributes"] = {**span_attributes, "exit_code": 0, "timed_out": False}
            recorder.close("succeeded")
            local_record = json.loads(trace_path.read_text(encoding="utf-8"))

        self.assertEqual({"run_id": "run-1", "model": "gpt-test"}, captured_run_attributes)
        self.assertEqual("a" * 32, local_record["trace_id"])
        self.assertEqual("b" * 16, local_record["parent_span_id"])
        self.assertEqual("0000000000000001", local_record["span_id"])
        self.assertEqual("bug_fix", local_record["attributes"]["agent_workflow.task.type"])
        self.assertEqual(0, local_record["attributes"]["process.exit.code"])
        self.assertEqual("agent_workflow.step.run_qc", session.started[0][0])
        self.assertEqual("OK", session.finished[0][1])
        self.assertEqual("succeeded", session.run_status)

    def test_trace_runtime_is_disabled_without_a_trace_endpoint(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(load_otlp_trace_runtime({"run_id": "run-1"}))

    def test_trace_recorder_exports_error_status_for_failed_attempt_and_run(self) -> None:
        session = FakeTraceSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = TraceRecorder(Path(temp_dir) / "trace.jsonl", otel_factory=lambda _attrs: session)
            with recorder.span("agent_workflow.step.run_qc", attempt=1) as span:
                span["status_code"] = "ERROR"
                span["status_message"] = "QC failed"
            recorder.close("qc_failed")

        self.assertEqual("ERROR", session.finished[0][1])
        self.assertEqual("QC failed", session.finished[0][2])
        self.assertEqual("qc_failed", session.run_status)

    def test_trace_exporter_none_overrides_a_generic_endpoint(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318", "OTEL_TRACES_EXPORTER": "none"},
            clear=True,
        ):
            self.assertIsNone(load_otlp_trace_runtime({"run_id": "run-1"}))
            self.assertNotIn("configured", trace_enabled_hint())


if __name__ == "__main__":
    unittest.main()
