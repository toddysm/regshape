#!/usr/bin/env python3

"""Framework-neutral telemetry sessions, import isolation, and log output."""

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
    telemetry_session,
    track_time,
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
        """A library session appends diagnostics and closes only its owned file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log",
                                          delete=False) as f:
            f.write("existing content\n")
            path = f.name

        try:
            primary = io.StringIO()
            config = TelemetryConfig(
                time_methods_enabled=True,
                output=primary,
                log_file_path=path,
                method_timings=[("appended_method", 0.2)],
            )
            previous = get_telemetry_config()
            with telemetry_session(config) as active:
                assert active is config
                assert get_telemetry_config() is config
                log_fh = config.log_file
                assert log_fh is not None and not log_fh.closed
            assert log_fh.closed
            assert config.log_file is log_fh
            assert get_telemetry_config() is previous

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


@pytest.mark.parametrize("module", ["regshape.libs", "regshape.libs.decorators"])
def test_library_import_does_not_load_cli_framework(module: str) -> None:
    """Fresh SDK imports must not transitively load Click or CLI modules."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, sys; importlib.import_module(sys.argv[1]); "
                "assert not any(n == 'click' or n.startswith('click.') "
                "or n == 'regshape.cli' or n.startswith('regshape.cli.') for n in sys.modules)"
            ),
            module,
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_session_borrows_streams_and_runs_library_decorators() -> None:
    """SDK callers get automatic timing output without surrendering their streams."""
    primary = io.StringIO()
    log = io.StringIO()
    config = TelemetryConfig(
        time_methods_enabled=True, output_format="json", output=primary, log_file=log,
    )
    previous = get_telemetry_config()

    @track_time
    def operation() -> int:
        """Provide a timed library operation with a visible return value."""
        return 42

    with telemetry_session(config):
        assert operation() == 42
    assert "operation" in primary.getvalue()
    assert primary.getvalue() == log.getvalue()
    assert not primary.closed and not log.closed
    assert config.log_file is log
    assert get_telemetry_config() is previous


@pytest.mark.parametrize("restore_inner", [False, True])
@pytest.mark.parametrize("restore_outer", [False, True])
def test_nested_sessions_flush_and_restore_their_own_configuration(
    tmp_path: Path, restore_inner: bool, restore_outer: bool,
) -> None:
    """Each scope flushes its own data/log even when a nested config remains active."""
    previous = get_telemetry_config()
    outer_output = io.StringIO()
    inner_output = io.StringIO()
    log_path = tmp_path / "outer.log"
    outer = TelemetryConfig(
        output=outer_output, method_timings=[("outer", 0.1)], log_file_path=str(log_path),
    )
    inner = TelemetryConfig(output=inner_output, method_timings=[("inner", 0.2)])
    retained = outer if restore_inner else inner
    try:
        with telemetry_session(outer, restore_config=restore_outer):
            with telemetry_session(inner, restore_config=restore_inner):
                assert get_telemetry_config() is inner
            assert get_telemetry_config() is retained
            assert "inner" in inner_output.getvalue()
            assert outer_output.getvalue() == ""
        assert "outer" in outer_output.getvalue()
        assert "inner" not in outer_output.getvalue()
        assert log_path.read_text() == outer_output.getvalue()
        assert get_telemetry_config() is (previous if restore_outer else retained)
    finally:
        configure_telemetry(previous)


def test_session_can_retain_config_for_outer_diagnostics() -> None:
    """Outer diagnostics can reuse a retained configuration after the session flushes."""
    previous = get_telemetry_config()
    output = io.StringIO()
    config = TelemetryConfig(output=output, output_format="json")
    try:
        with telemetry_session(config, restore_config=False):
            config.method_timings.append(("inner", 0.1))
        get_telemetry_config().method_timings.append(("outer", 0.2))
        flush_telemetry()
        assert [json.loads(line)["name"] for line in output.getvalue().splitlines()] == [
            "inner", "outer",
        ]
    finally:
        configure_telemetry(previous)


@pytest.mark.parametrize("owned_log", [False, True])
def test_session_rejects_closed_log_before_running_operation(
    tmp_path: Path, owned_log: bool,
) -> None:
    """Closed borrowed/retained logs fail on entry; resetting an owned log permits reuse."""
    previous = get_telemetry_config()
    output = io.StringIO()
    path = tmp_path / "telemetry.log"
    config = TelemetryConfig(
        output=output, output_format="json", method_timings=[("first", 0.1)],
        log_file_path=str(path) if owned_log else None,
        log_file=None if owned_log else io.StringIO(),
    )
    with telemetry_session(config):
        pass
    log = config.log_file
    assert log is not None
    if not owned_log:
        log.close()
    config.method_timings.append(("second", 0.2))
    with (
        pytest.raises(ValueError, match="Telemetry log stream is closed"),
        telemetry_session(config),
    ):
        pytest.fail("A closed log must be rejected before the operation runs")
    assert get_telemetry_config() is previous
    assert config.log_file is log
    assert config.method_timings == [("second", 0.2)]
    assert [json.loads(line)["name"] for line in output.getvalue().splitlines()] == ["first"]
    if owned_log:
        config.log_file = None
        with telemetry_session(config):
            pass
        assert [json.loads(line)["name"] for line in path.read_text().splitlines()] == [
            "first", "second",
        ]
        assert config.log_file is not None and config.log_file.closed
        assert get_telemetry_config() is previous


def test_retained_owned_log_requires_detaching_before_late_telemetry(tmp_path: Path) -> None:
    """Retention leaves the owned log closed; detaching it allows primary-only diagnostics."""
    previous = get_telemetry_config()
    output = io.StringIO()
    path = tmp_path / "telemetry.log"
    config = TelemetryConfig(
        output=output, output_format="json", log_file_path=str(path),
        method_timings=[("inner", 0.1)],
    )
    try:
        with telemetry_session(config, restore_config=False):
            pass
        assert get_telemetry_config() is config
        assert config.log_file is not None and config.log_file.closed
        config.method_timings.append(("late", 0.2))
        with pytest.raises(ValueError, match="closed file"):
            flush_telemetry()
        assert config.method_timings == []
        config.log_file = None
        config.method_timings.append(("detached", 0.3))
        flush_telemetry()
        assert [json.loads(line)["name"] for line in output.getvalue().splitlines()] == [
            "inner", "late", "detached",
        ]
        assert [json.loads(line)["name"] for line in path.read_text().splitlines()] == ["inner"]
    finally:
        configure_telemetry(previous)


@pytest.mark.parametrize("primary", [None, ValueError("operation"), SystemExit(3)])
def test_session_preserves_first_error_and_closes_log(
    primary: BaseException | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operation/exit errors win over flush and close errors; cleanup-only errors surface."""
    previous = get_telemetry_config()
    logfile = MagicMock(spec=io.TextIOBase)
    logfile.close.side_effect = OSError("close failed")
    flush_error = RuntimeError("flush failed")
    expected = primary if primary is not None else flush_error
    config = TelemetryConfig(log_file_path="synthetic.log")
    with (
        patch("regshape.libs.decorators.open", create=True, return_value=logfile),
        patch("regshape.libs.decorators.output.flush_telemetry", side_effect=flush_error),
        pytest.raises(type(expected)) as caught,
        telemetry_session(config),
    ):
        if primary is not None:
            raise primary
    assert caught.value is expected
    logfile.close.assert_called_once()
    assert config.log_file is logfile
    assert get_telemetry_config() is previous
    assert "Telemetry log cleanup" in caplog.text
    if primary is not None:
        assert "Telemetry flush" in caplog.text


def test_session_surfaces_close_only_failure() -> None:
    """A sole close failure propagates after restoring the outer configuration."""
    previous = get_telemetry_config()
    logfile = MagicMock(spec=io.TextIOBase)
    failure = OSError("close failed")
    logfile.close.side_effect = failure
    config = TelemetryConfig(log_file_path="synthetic.log")
    with (
        patch("regshape.libs.decorators.open", create=True, return_value=logfile),
        pytest.raises(OSError) as caught,
        telemetry_session(config),
    ):
        pass
    assert caught.value is failure
    assert config.log_file is logfile
    assert get_telemetry_config() is previous


def test_session_open_failure_does_not_activate_config(tmp_path: Path) -> None:
    """An unavailable log path fails before replacing the caller's telemetry scope."""
    previous = get_telemetry_config()
    config = TelemetryConfig(log_file_path=str(tmp_path / "missing" / "telemetry.log"))
    with pytest.raises(FileNotFoundError), telemetry_session(config):
        pytest.fail("A failed log acquisition must not enter the session")
    assert config.log_file is None
    assert get_telemetry_config() is previous
