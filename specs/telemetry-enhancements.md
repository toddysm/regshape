# Enhanced Telemetry and Debug Features

## Overview

This spec defines enhancements to the existing telemetry decorator infrastructure (`libs/decorators/`) to improve debug output, integrate with `--log-file`, support structured logging, add performance metrics, and introduce telemetry verbosity levels. The goal is to make RegShape's telemetry a first-class diagnostic tool for both interactive CLI use and automated test analysis.

**GitHub Issue:** [#34 — Enhanced Telemetry and Debug Features](https://github.com/toddysm/regshape/issues/34)

## Table of Contents

- [1. Current State](#1-current-state)
- [2. Log File Integration](#2-log-file-integration)
- [3. Enhanced Debug-Calls Formatting](#3-enhanced-debug-calls-formatting)
- [4. Break Mode Logging Integration](#4-break-mode-logging-integration)
- [5. Structured Logging Output](#5-structured-logging-output)
- [6. Performance Metrics Collection and Display](#6-performance-metrics-collection-and-display)
- [7. Telemetry Verbosity Levels](#7-telemetry-verbosity-levels)
- [8. Updated CLI Flags](#8-updated-cli-flags)
- [9. Updated TelemetryConfig](#9-updated-telemetryconfig)
- [10. Module Changes](#10-module-changes)
- [11. Implementation Sequence](#11-implementation-sequence)
- [12. Open Questions](#12-open-questions)

---

## 1. Current State

The existing telemetry system (see `architecture.md` Section 8) provides three decorators:

| Decorator | CLI Flag | Purpose |
|-----------|----------|---------|
| `@track_time` | `--time-methods` | Per-method execution timing |
| `@track_scenario` | `--time-scenarios` | Multi-step workflow timing |
| `@debug_call` | `--debug-calls` | HTTP request/response header logging (curl -v style) |

All telemetry output currently goes to `stderr` via `TelemetryConfig.output`. The global `--log-file` option exists in `cli/main.py` but is **not wired** into the telemetry system — it is stored in the Click context but never consumed.

### Gaps Addressed by This Spec

1. `--log-file` has no effect on telemetry output.
2. `--debug-calls` shows headers only — no response body preview, no per-call timing, no content-length summary.
3. No integration between break mode rule application and telemetry output.
4. No machine-readable (JSON) telemetry output format.
5. No aggregate performance metrics (request count, bytes transferred, error/retry counts).
6. Telemetry flags are binary (on/off) with no way to control detail level.

---

## 2. Log File Integration

### Requirement

When `--log-file <path>` is specified, **all telemetry output** (timing blocks, debug-calls, performance metrics) is written to the specified file **in addition to** stderr. This allows users to capture diagnostic data for later analysis while still seeing output interactively.

### Design

`TelemetryConfig` gains a `log_file_path` field. When set, `configure_telemetry()` opens the file for writing and stores the file handle in a new `log_file` field. A new helper, `telemetry_write()`, replaces direct `print(..., file=out)` calls in the output modules and writes to both `output` (stderr) and `log_file` (when present).

```python
@dataclass
class TelemetryConfig:
    # ... existing fields ...
    log_file_path: Optional[str] = None
    log_file: Optional[IO] = field(default=None, repr=False)
```

### Wiring

The `@telemetry_options` decorator in `libs/decorators/__init__.py` already consumes CLI kwargs before forwarding to the command. It will be extended to read the `--log-file` value from the Click context (`ctx.obj["log_file"]`) and pass it into `TelemetryConfig`:

```python
@functools.wraps(func)
def wrapper(*args, **kwargs):
    ctx = click.get_current_context()
    log_file_path = ctx.obj.get("log_file") if ctx.obj else None
    configure_telemetry(TelemetryConfig(
        time_methods_enabled=kwargs.pop("time_methods", False),
        time_scenarios_enabled=kwargs.pop("time_scenarios", False),
        debug_calls_enabled=kwargs.pop("debug_calls", False),
        log_file_path=log_file_path,
    ))
    ...
```

### Output Routing

A new function in `output.py`:

```python
def telemetry_write(line: str, out: IO = None, log_file: IO = None) -> None:
    """Write a telemetry line to stderr and optionally to the log file.

    :param line: Text to write (newline appended automatically).
    :param out: Primary output stream (defaults to stderr).
    :param log_file: Secondary output stream (log file), or None.
    """
    if out is None:
        out = sys.stderr
    print(line, file=out)
    if log_file is not None:
        print(line, file=log_file)
```

All existing `print(...)` calls in `output.py` and `call_details.py` are replaced with `telemetry_write()`.

### File Lifecycle

The log file is opened in **append mode** (`"a"`) when `configure_telemetry()` is called with a non-None `log_file_path`, and closed in the `finally` block of the `@telemetry_options` wrapper (alongside `flush_telemetry()`). Append mode ensures that consecutive commands don't overwrite previous output, which is useful when running a script of multiple `regshape` invocations.

```python
finally:
    from regshape.libs.decorators.output import flush_telemetry
    flush_telemetry()
    cfg = get_telemetry_config()
    if cfg.log_file is not None:
        cfg.log_file.close()
```

---

## 3. Enhanced Debug-Calls Formatting

### Requirement

Enhance `--debug-calls` to show richer per-call diagnostic information beyond the current curl-v header dump.

### New Elements

1. **Per-call elapsed time** — A `* Elapsed: 0.123s` line after the response block.
2. **Response body preview** — Truncated first N bytes of the response body when the content type is text-based (JSON, plain text, HTML). Binary content types show `* Body: <binary, 4096 bytes>`.
3. **Content-Length summary** — A `* Content-Length: 4096` line in the request block when a body is sent, and in the response summary.
4. **Separator between calls** — A blank line between consecutive debug-call blocks for readability.

### Updated Output Format

```
* Connected to registry.example.com port 443
> PUT /v2/myrepo/manifests/latest HTTP/1.1
> Host: registry.example.com
> Content-Type: application/vnd.oci.image.manifest.v1+json
> Content-Length: 1234
>
< HTTP/1.1 201 Created
< Docker-Content-Digest: sha256:abc123...
< Content-Length: 0
<
* Elapsed: 0.045s
* Body: (empty)

* Connected to registry.example.com port 443
> GET /v2/myrepo/manifests/latest HTTP/1.1
> Host: registry.example.com
> Accept: application/vnd.oci.image.manifest.v1+json
>
< HTTP/1.1 200 OK
< Content-Type: application/vnd.oci.image.manifest.v1+json
< Content-Length: 1234
<
* Elapsed: 0.112s
* Body: {"schemaVersion":2,"mediaType":"application/vnd.oci.image.manif...
```

### Implementation

`format_curl_debug()` gains new parameters:

```python
def format_curl_debug(
    method: str,
    url: str,
    req_headers: dict,
    status_code: int,
    reason: str,
    resp_headers: dict,
    out: IO = None,
    *,
    elapsed: Optional[float] = None,
    resp_body: Optional[bytes] = None,
    req_content_length: Optional[int] = None,
    verbosity: int = 1,
) -> None:
```

The `@debug_call` decorator wraps the underlying function call with `time.perf_counter()` to measure elapsed time, and extracts `resp_body` from the response object (limited to the first 200 bytes for preview).

### Body Preview Rules

| Content-Type Pattern | Preview |
|---------------------|---------|
| `application/json`, `*+json` | First 200 chars, UTF-8 decoded |
| `text/*` | First 200 chars, UTF-8 decoded |
| Binary / unknown | `<binary, N bytes>` |
| Empty body (Content-Length: 0 or no body) | `(empty)` |

Body preview is only shown at verbosity level ≥ 1 (see Section 7). The 200-char limit keeps output compact and avoids flooding the terminal with large manifests. At verbosity level 2, the full body is printed.

---

## 4. Break Mode Logging Integration

### Requirement

When break mode is active, telemetry output should capture:
- Which break rules matched and were applied to each request.
- What mutations were made (before/after for the mutated field).
- Rule application included in the telemetry summary block.

### Design

The `BreakModeMiddleware` (when implemented) will append entries to a new `TelemetryConfig.break_rule_log` list as it applies rules. Each entry records the rule name, the target field, the action, and optionally the original and mutated values.

```python
@dataclass
class BreakRuleEntry:
    """Record of a single break rule application.

    :param rule_name: Human-readable name of the BreakRule.
    :param target: Mutation target (header, body, method, path, etc.).
    :param action: Mutation action (replace, remove, corrupt, etc.).
    :param original: Original value of the mutated field (redacted if sensitive).
    :param mutated: New value after mutation (redacted if sensitive).
    """
    rule_name: str
    target: str
    action: str
    original: Optional[str] = None
    mutated: Optional[str] = None
```

```python
@dataclass
class TelemetryConfig:
    # ... existing fields ...
    break_rule_log: list[BreakRuleEntry] = field(default_factory=list)
```

### Debug-Calls Integration

When `--debug-calls` is active and break rules were applied to the current request, the debug output includes a `* Break:` section before the response:

```
* Connected to registry.example.com port 443
> PUT /v2/myrepo/manifests/latest HTTP/1.1
> Host: registry.example.com
> Content-Type: text/plain
>
* Break: invalid_content_type — replaced Content-Type
*   original: application/vnd.oci.image.manifest.v1+json
*   mutated:  text/plain
* Break: skip_auth — removed Authorization
<
< HTTP/1.1 401 Unauthorized
< Www-Authenticate: Bearer realm="..."
<
* Elapsed: 0.023s
```

### Telemetry Block Integration

The telemetry summary block gains a `break` row type when break rule entries are present:

```
── telemetry ──────────────────────────────────────────────────────
  scenario  manifest put                                  0.045s
    method  push_manifest                                 0.044s
     break  invalid_content_type                     1 applied
     break  skip_auth                                1 applied
───────────────────────────────────────────────────────────────────
```

After rendering, `break_rule_log` is cleared alongside `method_timings`.

### Sensitive Value Redaction

When a break rule mutates auth-related fields (`target == "auth"`, or `key` is a sensitive header), the `original` and `mutated` values in `BreakRuleEntry` are redacted using the existing `redact_header_value()` function before being stored.

---

## 5. Structured Logging Output

### Requirement

Support a machine-readable JSON format for telemetry output, suitable for automated analysis pipelines and test result post-processing.

### CLI Flag

A new `--telemetry-format` option controls the output format:

| Value | Description |
|-------|-------------|
| `text` (default) | Current human-readable block format |
| `json` | One JSON object per telemetry event, newline-delimited (NDJSON) |

### TelemetryConfig Addition

```python
@dataclass
class TelemetryConfig:
    # ... existing fields ...
    output_format: str = "text"   # "text" or "json"
```

### JSON Schema

Each telemetry event is a self-contained JSON object emitted on a single line:

#### Scenario Event

```json
{
  "type": "scenario",
  "name": "manifest get",
  "elapsed_s": 0.523,
  "methods": [
    {"name": "get_manifest", "elapsed_s": 0.231},
    {"name": "resolve_credentials", "elapsed_s": 0.045}
  ],
  "break_rules": [],
  "timestamp": "2026-03-05T10:30:00.123Z"
}
```

#### Debug-Call Event

```json
{
  "type": "debug_call",
  "request": {
    "method": "GET",
    "url": "https://registry.example.com/v2/myrepo/manifests/latest",
    "headers": {"Accept": "application/vnd.oci.image.manifest.v1+json", "Authorization": "Bearer <redacted>"}
  },
  "response": {
    "status_code": 200,
    "reason": "OK",
    "headers": {"Content-Type": "application/vnd.oci.image.manifest.v1+json", "Content-Length": "1234"},
    "body_preview": "{\"schemaVersion\":2,...}",
    "body_size": 1234
  },
  "elapsed_s": 0.112,
  "break_rules_applied": [],
  "timestamp": "2026-03-05T10:30:00.234Z"
}
```

#### Performance Metrics Event

```json
{
  "type": "metrics",
  "total_requests": 3,
  "total_elapsed_s": 0.523,
  "total_bytes_sent": 1234,
  "total_bytes_received": 5678,
  "retries": 1,
  "errors": 0,
  "status_code_counts": {"200": 2, "401": 1},
  "timestamp": "2026-03-05T10:30:00.345Z"
}
```

### Rendering Path

`output.py` gains a `_render_json()` counterpart to the existing text rendering functions. The `print_telemetry_block()` function checks `config.output_format` and dispatches to the appropriate renderer:

```python
def print_telemetry_block(
    scenario_name, scenario_elapsed, method_timings, out=None,
    *, break_entries=None, metrics=None
):
    config = get_telemetry_config()
    if config.output_format == "json":
        _render_json_block(scenario_name, scenario_elapsed, method_timings,
                           break_entries, metrics, out, config.log_file)
    else:
        _render_text_block(scenario_name, scenario_elapsed, method_timings,
                           break_entries, metrics, out, config.log_file)
```

---

## 6. Performance Metrics Collection and Display

### Requirement

Aggregate and display performance metrics across the entire command invocation: request count, bytes transferred, status code distribution, retry counts, and error counts.

### PerformanceMetrics Dataclass

A new dataclass in `libs/decorators/__init__.py`:

```python
@dataclass
class PerformanceMetrics:
    """Aggregate metrics collected during a command invocation.

    :param total_requests: Total number of HTTP requests made.
    :param total_bytes_sent: Total request body bytes sent.
    :param total_bytes_received: Total response body bytes received.
    :param retries: Number of retried requests (e.g. 401 re-auth).
    :param errors: Number of requests that resulted in 4xx/5xx status codes.
    :param status_code_counts: Counter of status codes seen.
    :param total_elapsed: Wall-clock time for all HTTP calls combined.
    """
    total_requests: int = 0
    total_bytes_sent: int = 0
    total_bytes_received: int = 0
    retries: int = 0
    errors: int = 0
    status_code_counts: dict[int, int] = field(default_factory=dict)
    total_elapsed: float = 0.0

    def record_request(
        self,
        status_code: int,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        elapsed: float = 0.0,
        is_retry: bool = False,
    ) -> None:
        """Record a single HTTP request into the aggregated metrics.

        :param status_code: HTTP response status code.
        :param bytes_sent: Request body size in bytes.
        :param bytes_received: Response body size in bytes.
        :param elapsed: Elapsed time in seconds for this request.
        :param is_retry: Whether this request was a retry.
        """
        self.total_requests += 1
        self.total_bytes_sent += bytes_sent
        self.total_bytes_received += bytes_received
        self.total_elapsed += elapsed
        self.status_code_counts[status_code] = self.status_code_counts.get(status_code, 0) + 1
        if is_retry:
            self.retries += 1
        if status_code >= 400:
            self.errors += 1
```

### TelemetryConfig Addition

```python
@dataclass
class TelemetryConfig:
    # ... existing fields ...
    metrics_enabled: bool = False
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
```

### Collection Point

The `@debug_call` decorator is the natural collection point since it already wraps every HTTP call. When `metrics_enabled` is True, each call records into `config.metrics`:

```python
# Inside @debug_call wrapper, after the HTTP call returns:
if config.metrics_enabled:
    config.metrics.record_request(
        status_code=result.status_code,
        bytes_sent=req_content_length or 0,
        bytes_received=int(result.headers.get('Content-Length', 0)),
        elapsed=elapsed,
    )
```

This means `--metrics` implies `--debug-calls` instrumentation internally. However, the two flags remain independent for output purposes: `--metrics` alone shows only the summary block, while `--debug-calls` shows per-call details. When both are active, the user sees both.

### CLI Flag

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--metrics` | flag | false | Display aggregate performance metrics after the command completes. |

### Display Format (Text)

A new summary section at the end of the telemetry block:

```
── telemetry ──────────────────────────────────────────────────────
  scenario  manifest get                                  0.523s
    method  get_manifest                                  0.231s
    method  resolve_credentials                           0.045s
   metrics  requests: 3  sent: 1.2 KB  recv: 5.5 KB
   metrics  status: 200×2  401×1  retries: 1  errors: 0
───────────────────────────────────────────────────────────────────
```

### Display Format (JSON)

See the `metrics` event type in Section 5.

### Flush Lifecycle

`flush_telemetry()` emits the metrics summary after the method timings, then resets `config.metrics` to a fresh `PerformanceMetrics()`.

---

## 7. Telemetry Verbosity Levels

### Requirement

Replace the binary on/off granularity with numeric verbosity levels that control how much detail each telemetry feature produces.

### Levels

| Level | Name | Description |
|-------|------|-------------|
| 0 | Off | Telemetry feature disabled (current default behavior) |
| 1 | Normal | Standard output — same as current on behavior |
| 2 | Verbose | Extended output — full response bodies, per-call timings inline with methods, header values untruncated |

### How Verbosity Applies to Each Feature

| Feature | Level 0 | Level 1 | Level 2 |
|---------|---------|---------|---------|
| `--time-methods` | No output | Method name + elapsed (current) | Method name + elapsed + call count if called multiple times |
| `--time-scenarios` | No output | Scenario name + elapsed + nested methods (current) | Same + percentage breakdown per method |
| `--debug-calls` | No output | Headers + elapsed + body preview (200 chars) | Headers + elapsed + full response body (unlimited) |
| `--metrics` | No output | Summary line (request count, bytes, status codes) | Summary + per-request breakdown table |

### CLI Mechanism

The existing boolean flags are preserved for backwards compatibility. A new `--telemetry-verbosity` option provides fine-grained control:

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--telemetry-verbosity` | `-tv` | int | 1 | Verbosity level (0, 1, 2) for telemetry output when a telemetry flag is active. |

When a boolean flag (e.g. `--time-methods`) is present **without** `--telemetry-verbosity`, level 1 is used. When `--telemetry-verbosity=2` is specified, any active telemetry flag uses level 2.

### TelemetryConfig Addition

```python
@dataclass
class TelemetryConfig:
    # ... existing fields ...
    verbosity: int = 1   # 0=off, 1=normal, 2=verbose
```

---

## 8. Updated CLI Flags

### Leaf-Command Telemetry Flags (via `@telemetry_options`)

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--time-methods` | | flag | false | Print execution time for individual method calls. |
| `--time-scenarios` | | flag | false | Print execution time for multi-step workflows. |
| `--debug-calls` | | flag | false | Print request/response details for each HTTP call. |
| `--metrics` | | flag | false | Display aggregate performance metrics. |
| `--telemetry-format` | | choice | text | Telemetry output format: `text` or `json`. |
| `--telemetry-verbosity` | `-tv` | int | 1 | Verbosity level for telemetry output (0, 1, 2). |

### Global Flag (in `cli/main.py`)

| Option | Type | Description |
|--------|------|-------------|
| `--log-file` | path | Path for telemetry log file (append mode). Already exists; now wired to TelemetryConfig. |

### Backwards Compatibility

All existing flags and their default behavior are preserved. The new flags are additive. Running `regshape manifest get -i ref --time-methods` produces identical output to the current implementation.

---

## 9. Updated TelemetryConfig

The full updated dataclass:

```python
@dataclass
class TelemetryConfig:
    """Runtime configuration for telemetry decorators.

    :param time_methods_enabled: When True, @track_time accumulates per-method
        timing entries.
    :param time_scenarios_enabled: When True, @track_scenario renders the
        telemetry summary block.
    :param debug_calls_enabled: When True, @debug_call prints each HTTP
        round-trip.
    :param metrics_enabled: When True, aggregate performance metrics are
        collected and displayed.
    :param verbosity: Detail level for telemetry output (0=off, 1=normal,
        2=verbose).
    :param output_format: Output format — "text" (default) or "json".
    :param output: Writable stream for telemetry output (defaults to stderr).
    :param log_file_path: Optional file path for telemetry log output.
    :param log_file: File handle for log_file_path (managed by telemetry_options).
    :param method_timings: Accumulated (qualname, elapsed) pairs from @track_time.
    :param break_rule_log: Break rule application records from BreakModeMiddleware.
    :param metrics: Aggregate performance metrics.
    """
    time_methods_enabled: bool = False
    time_scenarios_enabled: bool = False
    debug_calls_enabled: bool = False
    metrics_enabled: bool = False
    verbosity: int = 1
    output_format: str = "text"
    output: IO = field(default_factory=lambda: sys.stderr)
    log_file_path: Optional[str] = None
    log_file: Optional[IO] = field(default=None, repr=False)
    method_timings: list[tuple[str, float]] = field(default_factory=list)
    break_rule_log: list['BreakRuleEntry'] = field(default_factory=list)
    metrics: 'PerformanceMetrics' = field(default_factory=PerformanceMetrics)
```

---

## 10. Module Changes

### Files to Modify

| File | Changes |
|------|---------|
| `libs/decorators/__init__.py` | Add `PerformanceMetrics`, `BreakRuleEntry` dataclasses; extend `TelemetryConfig` with new fields; update `telemetry_options` to add `--metrics`, `--telemetry-format`, `--telemetry-verbosity` options and wire `--log-file` from context; add log file open/close lifecycle. |
| `libs/decorators/output.py` | Add `telemetry_write()` helper; update `print_telemetry_block()` to accept break entries and metrics, dispatch to text/JSON renderers; add `_render_json_block()` for JSON mode; update `flush_telemetry()` to emit metrics and close log file. |
| `libs/decorators/call_details.py` | Update `format_curl_debug()` signature with `elapsed`, `resp_body`, `req_content_length`, `verbosity` params; add per-call timing line, body preview, content-length; add break rule `* Break:` lines; add metrics recording; update `@debug_call` to measure elapsed time, extract body preview, record metrics. |
| `libs/decorators/timing.py` | No changes to core logic. `@track_time` is already minimal and correct. |
| `libs/decorators/scenario.py` | Update `print_telemetry_block()` call to pass `break_entries` and `metrics`. |
| `cli/main.py` | No changes (global `--log-file` is already stored in `ctx.obj`). |
| CLI leaf commands | No changes (new options are injected by `@telemetry_options`). |

### New File

| File | Purpose |
|------|---------|
| `libs/decorators/metrics.py` | `PerformanceMetrics` dataclass and `BreakRuleEntry` dataclass, kept in a separate module to avoid bloating `__init__.py`. Exported from `__init__.py`. |

### Test Files to Create/Modify

| File | Changes |
|------|---------|
| `tests/test_telemetry_log_file.py` | Tests for log file creation, append mode, dual output (stderr + file), file close on cleanup. |
| `tests/test_debug_calls_enhanced.py` | Tests for elapsed time line, body preview (JSON, text, binary, empty), content-length line, verbosity levels. |
| `tests/test_telemetry_metrics.py` | Tests for `PerformanceMetrics.record_request()`, metrics rendering in text and JSON, flush lifecycle. |
| `tests/test_telemetry_json_output.py` | Tests for JSON schema conformance of scenario, debug_call, and metrics events. |

---

## 11. Implementation Sequence

Work is ordered to minimize risk and enable incremental testing.

### Step 1: `PerformanceMetrics` and `BreakRuleEntry` Dataclasses

Create `libs/decorators/metrics.py` with both dataclasses. Unit-test `PerformanceMetrics.record_request()`. Export from `libs/decorators/__init__.py`.

### Step 2: Extend `TelemetryConfig`

Add new fields to `TelemetryConfig`. No behavioral changes yet — new fields default to disabled/empty.

### Step 3: Log File Integration

1. Update `telemetry_options` to read `--log-file` from context and open the file.
2. Add `telemetry_write()` to `output.py`.
3. Replace `print()` calls with `telemetry_write()` in `output.py` and `call_details.py`.
4. Add file close in the `finally` block.
5. Test: verify dual output and file lifecycle.

### Step 4: Enhanced Debug-Calls Formatting

1. Extend `format_curl_debug()` signature.
2. Update `@debug_call` to measure elapsed time and extract body preview.
3. Add per-call timing line, body preview, and content-length lines.
4. Test: verify new output elements appear.

### Step 5: Performance Metrics Collection

1. Add `--metrics` to `@telemetry_options`.
2. Wire metrics recording into `@debug_call`.
3. Add metrics rendering to `print_telemetry_block()` and `flush_telemetry()`.
4. Test: verify metrics accuracy and rendering.

### Step 6: Structured JSON Output

1. Add `--telemetry-format` to `@telemetry_options`.
2. Implement `_render_json_block()` in `output.py`.
3. Update `format_curl_debug()` to emit JSON when format is `"json"`.
4. Test: verify JSON schema conformance.

### Step 7: Telemetry Verbosity Levels

1. Add `--telemetry-verbosity` to `@telemetry_options`.
2. Update rendering functions to respect verbosity levels.
3. Test: verify each level produces expected detail.

### Step 8: Break Mode Logging Integration

1. Add `* Break:` output to `format_curl_debug()` (conditioned on `break_rule_log` being non-empty).
2. Add `break` row type to `print_telemetry_block()`.
3. Defer BreakModeMiddleware population of `break_rule_log` to the break mode implementation (separate issue).
4. Test: verify rendering with synthetic `BreakRuleEntry` data.

---

## 12. Open Questions

- [ ] Should `--log-file` in append mode include a session separator (e.g. timestamp header) between invocations?
- [ ] Should `--telemetry-format json` emit one JSON object per event (NDJSON) or wrap all events in a single JSON array? NDJSON is simpler for streaming; an array is simpler for loading as a document. This spec assumes NDJSON.
- [ ] Should `--metrics` automatically enable the internal instrumentation of `@debug_call` (without showing per-call output), or should it require `--debug-calls` to be explicitly set? This spec assumes `--metrics` enables internal instrumentation independently.
- [ ] Should body preview in `--debug-calls` respect a configurable max-length, or is the fixed 200-char / unlimited (verbosity 2) split sufficient?
- [ ] For `--telemetry-verbosity`, should per-feature verbosity overrides (e.g. `--debug-calls-verbosity 2 --time-methods-verbosity 1`) be supported, or is a single global verbosity level enough for v1?
