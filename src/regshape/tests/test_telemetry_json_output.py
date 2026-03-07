#!/usr/bin/env python3

"""Tests for structured JSON telemetry output."""

import io
import json

from regshape.libs.decorators import TelemetryConfig, configure_telemetry
from regshape.libs.decorators.output import print_telemetry_block, flush_telemetry
from regshape.libs.decorators.call_details import format_curl_debug_json
from regshape.libs.decorators.metrics import PerformanceMetrics


class TestScenarioJsonOutput:
    """Tests for JSON-format scenario telemetry blocks."""

    def test_scenario_event_schema(self):
        buf = io.StringIO()
        configure_telemetry(TelemetryConfig(output_format="json", output=buf))
        print_telemetry_block(
            "manifest get", 0.523,
            [("get_manifest", 0.231), ("resolve_credentials", 0.045)],
            buf,
        )
        line = buf.getvalue().strip()
        event = json.loads(line)
        assert event["type"] == "scenario"
        assert event["name"] == "manifest get"
        assert event["elapsed_s"] == 0.523
        assert len(event["methods"]) == 2
        assert event["methods"][0]["name"] == "get_manifest"
        assert event["methods"][0]["elapsed_s"] == 0.231
        assert "timestamp" in event

    def test_method_only_events(self):
        """Methods without a scenario emit individual method events."""
        buf = io.StringIO()
        configure_telemetry(TelemetryConfig(output_format="json", output=buf))
        print_telemetry_block(
            None, None,
            [("func_a", 0.1), ("func_b", 0.2)],
            buf,
        )
        lines = [l for l in buf.getvalue().strip().split('\n') if l]
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        e2 = json.loads(lines[1])
        assert e1["type"] == "method"
        assert e1["name"] == "func_a"
        assert e2["type"] == "method"
        assert e2["name"] == "func_b"

    def test_empty_data_emits_nothing(self):
        buf = io.StringIO()
        configure_telemetry(TelemetryConfig(output_format="json", output=buf))
        print_telemetry_block(None, None, [], buf)
        assert buf.getvalue() == ""


class TestMetricsJsonOutput:
    """Tests for JSON-format metrics events."""

    def test_metrics_event_schema(self):
        buf = io.StringIO()
        metrics = PerformanceMetrics()
        metrics.record_request(200, bytes_sent=100, bytes_received=500, elapsed=0.1)
        metrics.record_request(401, bytes_sent=0, bytes_received=0, elapsed=0.02)
        metrics.record_request(200, bytes_sent=50, bytes_received=300, elapsed=0.15)

        configure_telemetry(TelemetryConfig(output_format="json", output=buf))
        print_telemetry_block(
            "test scenario", 0.5,
            [("some_method", 0.1)],
            buf,
            metrics=metrics,
        )
        lines = [l for l in buf.getvalue().strip().split('\n') if l]
        assert len(lines) == 2  # scenario + metrics

        metrics_event = json.loads(lines[1])
        assert metrics_event["type"] == "metrics"
        assert metrics_event["total_requests"] == 3
        assert metrics_event["total_bytes_sent"] == 150
        assert metrics_event["total_bytes_received"] == 800
        assert metrics_event["retries"] == 0
        assert metrics_event["errors"] == 1
        assert metrics_event["status_code_counts"]["200"] == 2
        assert metrics_event["status_code_counts"]["401"] == 1
        assert "timestamp" in metrics_event

    def test_no_metrics_event_when_empty(self):
        buf = io.StringIO()
        metrics = PerformanceMetrics()  # no requests recorded
        configure_telemetry(TelemetryConfig(output_format="json", output=buf))
        print_telemetry_block(
            "test scenario", 0.5,
            [("some_method", 0.1)],
            buf,
            metrics=metrics,
        )
        lines = [l for l in buf.getvalue().strip().split('\n') if l]
        assert len(lines) == 1  # scenario only, no metrics


class TestDebugCallJsonOutput:
    """Tests for JSON-format debug_call events."""

    def test_debug_call_event_schema(self):
        buf = io.StringIO()
        format_curl_debug_json(
            method="GET",
            url="https://registry.example.com/v2/myrepo/manifests/latest",
            req_headers={"Accept": "application/json"},
            status_code=200,
            reason="OK",
            resp_headers={"Content-Type": "application/json", "Content-Length": "42"},
            out=buf,
            elapsed=0.112,
            resp_body=b'{"schemaVersion": 2}',
            req_content_length=None,
        )
        line = buf.getvalue().strip()
        event = json.loads(line)
        assert event["type"] == "debug_call"
        assert event["request"]["method"] == "GET"
        assert event["request"]["url"] == "https://registry.example.com/v2/myrepo/manifests/latest"
        assert event["response"]["status_code"] == 200
        assert event["response"]["reason"] == "OK"
        assert event["elapsed_s"] == 0.112
        assert "timestamp" in event

    def test_debug_call_body_preview_in_json(self):
        buf = io.StringIO()
        format_curl_debug_json(
            method="GET",
            url="https://registry.example.com/v2/test",
            req_headers={},
            status_code=200,
            reason="OK",
            resp_headers={"Content-Type": "application/json"},
            out=buf,
            resp_body=b'{"hello": "world"}',
        )
        event = json.loads(buf.getvalue().strip())
        assert event["response"]["body_preview"] == '{"hello": "world"}'
        assert event["response"]["body_size"] == 18

    def test_debug_call_req_content_length_in_json(self):
        buf = io.StringIO()
        format_curl_debug_json(
            method="PUT",
            url="https://registry.example.com/v2/test",
            req_headers={},
            status_code=201,
            reason="Created",
            resp_headers={},
            out=buf,
            req_content_length=512,
        )
        event = json.loads(buf.getvalue().strip())
        assert event["request"]["content_length"] == 512

    def test_debug_call_redacts_auth_in_json(self):
        buf = io.StringIO()
        format_curl_debug_json(
            method="GET",
            url="https://registry.example.com/v2/",
            req_headers={"Authorization": "Bearer my-secret-token"},
            status_code=200,
            reason="OK",
            resp_headers={},
            out=buf,
        )
        event = json.loads(buf.getvalue().strip())
        assert "my-secret-token" not in event["request"]["headers"]["Authorization"]
        assert "<redacted>" in event["request"]["headers"]["Authorization"]

    def test_debug_call_json_dual_output(self):
        buf = io.StringIO()
        log = io.StringIO()
        format_curl_debug_json(
            method="GET",
            url="https://registry.example.com/v2/",
            req_headers={},
            status_code=200,
            reason="OK",
            resp_headers={},
            out=buf,
            log_file=log,
        )
        assert buf.getvalue() == log.getvalue()
        assert json.loads(buf.getvalue().strip())["type"] == "debug_call"


class TestFlushTelemetryJson:
    """Tests for flush_telemetry in JSON mode."""

    def test_flush_emits_json_methods(self):
        buf = io.StringIO()
        configure_telemetry(TelemetryConfig(
            time_methods_enabled=True,
            output_format="json",
            output=buf,
            method_timings=[("my_func", 0.1)],
        ))
        flush_telemetry()
        line = buf.getvalue().strip()
        event = json.loads(line)
        assert event["type"] == "method"
        assert event["name"] == "my_func"

    def test_flush_emits_json_metrics(self):
        buf = io.StringIO()
        metrics = PerformanceMetrics()
        metrics.record_request(200, elapsed=0.1)
        configure_telemetry(TelemetryConfig(
            metrics_enabled=True,
            output_format="json",
            output=buf,
            metrics=metrics,
        ))
        flush_telemetry()
        line = buf.getvalue().strip()
        event = json.loads(line)
        assert event["type"] == "metrics"
        assert event["total_requests"] == 1
