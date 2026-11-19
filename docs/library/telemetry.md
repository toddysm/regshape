# Library telemetry

<!-- doc-type: reference -->

Import telemetry from `regshape.libs.decorators`. It does not import Click or
the command-line interface (CLI); Python callers do not need a command context.
The package still installs Click for its CLI.

## Managed telemetry scopes

`telemetry_session(config)` returns a synchronous context manager that activates
the supplied `TelemetryConfig` and returns that same configuration on entry.
Decorated operations inside the scope use it automatically:

```python
from regshape.libs.decorators import TelemetryConfig, telemetry_session, track_time


@track_time
def operation() -> int:
    """Run one operation whose duration is recorded by the library."""
    return 42


config = TelemetryConfig(
    time_methods_enabled=True,
    output_format="json",
    log_file_path="telemetry.log",
)
with telemetry_session(config):
    result = operation()
print(result)
```

This prints `42` to stdout. Timing goes to stderr by default and is also
appended to `telemetry.log` as a JSON method event. Set `output` to a writable
text stream to redirect diagnostics; its lifetime remains yours.

On every exit, the session flushes any remaining timings and enabled metrics,
closes its owned log, and by default restores the previous active configuration. Nested
sessions therefore return to their parent's configuration. Timings already
consumed by `track_scenario` are not repeated. Final flushing uses the owning
session's configuration even when a nested scope retains another configuration.

- If `log_file_path` is set and `log_file` is `None`, the session opens the
  path in append mode. The parent directory must exist. On cleanup it closes
  this stream and retains its closed handle on `config.log_file` for outer
  cleanup inspection. Use a fresh configuration or reset `log_file` to `None`
  before opening another owned-file session with the same configuration.
- If you supply `log_file`, it is borrowed, not closed; `log_file_path` is not
  opened. The primary `output` stream is also never closed by the session.
- A log-open error propagates before the configuration becomes active.
- Operation errors, including cancellation/exit exceptions, propagate after
  cleanup. Secondary flush/close failures are logged without replacing the
  original error. If only cleanup fails, its first error propagates.

## Manual configuration and CLI integration

`configure_telemetry(config)` only activates a configuration; it does not open
files or flush/close anything. `get_telemetry_config()` returns the active
configuration. Use `flush_telemetry()` when managing a scope manually.

For an outer diagnostic scope that must continue using the same configuration
after cleanup, pass `restore_config=False` to `telemetry_session`. The supplied
configuration then remains active until explicitly replaced; log ownership and
final flushing are unchanged.

The Click-only `telemetry_options` decorator lives in `regshape.cli.telemetry`,
not in the library. It maps the existing six
[telemetry options](../../specs/telemetry-enhancements.md#8-updated-cli-flags)
and root `--log-file` value into a library session. Option names, defaults,
output formats and exit codes are unchanged. The adapter uses
`restore_config=False` so existing outer result/warning scopes retain access
to the command configuration after the owned log closes.
