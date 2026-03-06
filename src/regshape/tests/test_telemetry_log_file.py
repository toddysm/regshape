#!/usr/bin/env python3

"""Tests for log file integration in telemetry."""

import io
import os
import tempfile

from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
)
from regshape.libs.decorators.output import (
    flush_telemetry,
    print_telemetry_block,
    telemetry_write,
)
from regshape.libs.decorators.metrics import PerformanceMetrics


class TestTelemetryWrite:
    """Tests for the telemetry_write() helper."""

    def test_writes_to_primary_stream(self):
        buf = io.StringIO()
        telemetry_write("hello", out=buf)
        assert buf.getvalue() == "hello\n"

    def test_writes_to_both_streams(self):
        primary = io.StringIO()
        secondary = io.StringIO()
        telemetry_write("hello", out=primary, log_file=secondary)
        assert primary.getvalue() == "hello\n"
        assert secondary.getvalue() == "hello\n"

    def test_none_log_file_is_safe(self):
        buf = io.StringIO()
        telemetry_write("hello", out=buf, log_file=None)
        assert buf.getvalue() == "hello\n"

    def test_defaults_to_stderr(self):
        """telemetry_write with no args should not raise."""
        # We can't easily capture stderr here, but we can verify no exception
        telemetry_write("test line")


class TestLogFileIntegration:
    """Tests for log file creation, dual output, and lifecycle."""

    def test_print_telemetry_block_writes_to_log_file(self):
        """Telemetry block output goes to both primary and log file."""
        primary = io.StringIO()
        log = io.StringIO()
        configure_telemetry(TelemetryConfig(
            time_methods_enabled=True,
            output=primary,
            log_file=log,
        ))
        print_telemetry_block(
            "test scenario", 1.234,
            [("my_method", 0.5)],
            primary,
        )
        primary_text = primary.getvalue()
        log_text = log.getvalue()
        assert "── telemetry" in primary_text
        assert "test scenario" in primary_text
        assert "── telemetry" in log_text
        assert "test scenario" in log_text

    def test_flush_telemetry_writes_to_log_file(self):
        """flush_telemetry sends output to both primary and log file."""
        primary = io.StringIO()
        log = io.StringIO()
        configure_telemetry(TelemetryConfig(
            time_methods_enabled=True,
            output=primary,
            log_file=log,
            method_timings=[("some_func", 0.1)],
        ))
        flush_telemetry()
        assert "some_func" in primary.getvalue()
        assert "some_func" in log.getvalue()

    def test_log_file_append_mode(self):
        """Log file opened via TelemetryConfig uses append mode."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log",
                                          delete=False) as f:
            f.write("existing content\n")
            path = f.name

        try:
            # Simulate what telemetry_options does
            log_fh = open(path, "a")
            primary = io.StringIO()
            configure_telemetry(TelemetryConfig(
                time_methods_enabled=True,
                output=primary,
                log_file_path=path,
                log_file=log_fh,
                method_timings=[("appended_method", 0.2)],
            ))
            flush_telemetry()
            log_fh.close()

            with open(path) as f:
                content = f.read()
            assert "existing content" in content
            assert "appended_method" in content
        finally:
            os.unlink(path)

    def test_metrics_in_log_file(self):
        """Metrics output is written to the log file."""
        primary = io.StringIO()
        log = io.StringIO()
        metrics = PerformanceMetrics()
        metrics.record_request(status_code=200, bytes_sent=100, bytes_received=500)
        configure_telemetry(TelemetryConfig(
            metrics_enabled=True,
            output=primary,
            log_file=log,
            metrics=metrics,
        ))
        flush_telemetry()
        assert "requests: 1" in primary.getvalue()
        assert "requests: 1" in log.getvalue()
