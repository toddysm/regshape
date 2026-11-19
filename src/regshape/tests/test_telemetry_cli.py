"""Thin Click wiring checks for the library-managed telemetry lifecycle."""

import json
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from regshape.cli.telemetry import telemetry_options
from regshape.libs.decorators import (
    TelemetryConfig,
    configure_telemetry,
    get_telemetry_config,
    track_time,
)
from regshape.tests.test_artifact_options import runner as runner  # noqa: PLC0414 - Click 8.1 compatibility


@pytest.fixture(autouse=True)
def reset_telemetry() -> Iterator[None]:
    """Keep retained command configurations and Click captures local to each test."""
    previous = get_telemetry_config()
    configure_telemetry(TelemetryConfig())
    yield
    configure_telemetry(previous)


@pytest.mark.parametrize("verbosity", [0, 1, 2])
def test_telemetry_flags_are_consumed_and_delegate_log_lifecycle(
    runner: CliRunner, tmp_path: Path, verbosity: int,
) -> None:
    """All six options configure the SDK scope without leaking into callback kwargs/stdout."""
    path = tmp_path / "telemetry.log"
    enabled = verbosity > 0

    @click.command()
    @telemetry_options
    @click.option("--value", default="kept")
    @track_time
    def command(value: str) -> None:
        """Observe parsed settings inside the real library-owned configuration scope."""
        config = get_telemetry_config()
        assert config.time_methods_enabled is enabled
        assert config.time_scenarios_enabled is enabled
        assert config.debug_calls_enabled is enabled
        assert config.metrics_enabled is enabled
        assert config.verbosity == verbosity
        assert config.output_format == "json"
        assert config.log_file_path == str(path)
        assert config.log_file is not None and not config.log_file.closed
        click.echo(value)

    result = runner.invoke(
        command,
        [
            "--time-methods", "--time-scenarios", "--debug-calls", "--metrics",
            "--telemetry-format", "JSON", "-tv", str(verbosity),
        ],
        obj={"log_file": str(path)},
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "kept\n"
    assert path.read_text() == result.stderr
    if enabled:
        event = json.loads(result.stderr)
        assert event["type"] == "method" and event["name"].endswith("command")
    else:
        assert result.stderr == ""
