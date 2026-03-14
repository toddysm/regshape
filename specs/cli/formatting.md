# CLI: Output Formatting Enhancements

## Overview

Create a centralized `cli/formatting.py` module with reusable output helpers that replace the duplicated patterns currently scattered across CLI modules. This improves consistency, reduces code duplication, and prepares for richer output (progress indicators, table formatting).

## Problem Statement

The current CLI modules exhibit several repeated patterns:

1. **Duplicated `_write()` helper** — Identical implementations in `manifest.py`, `tag.py`, `catalog.py`, and `referrer.py`.
2. **Duplicated `_emit_error()` helper** — Nearly identical `click.echo(f"Error [{ref}]: {reason}", err=True)` + `sys.exit(code)` across all modules, with minor format variations in `auth.py` and `ping.py`.
3. **Inconsistent JSON formatting** — Most modules use `json.dumps(obj, indent=2)`, but the pattern is manually applied everywhere.
4. **No progress indicators** — Long-running operations (blob upload, layout push) provide no feedback.
5. **No structured table output** — List commands (`tag list`, `catalog list`, `referrer list`) use raw `"\n".join()` or hand-crafted space-separated columns.

## API Surface

### Module: `src/regshape/cli/formatting.py`

All output helpers live in a single module. No classes — just functions.

---

#### `emit_json(data: dict | list, output_path: str | None = None) -> None`

Format and emit a JSON object. Always uses 2-space indentation.

**Parameters:**
- `data` — Serializable dict or list.
- `output_path` — If provided, write to file instead of stdout.

**Behavior:**
- Calls `json.dumps(data, indent=2)`.
- If `output_path` is set, writes to file (appending `\n` if missing), otherwise `click.echo()` to stdout.

---

#### `emit_text(content: str, output_path: str | None = None) -> None`

Emit plain text content to stdout or a file.

**Parameters:**
- `content` — Text string to output.
- `output_path` — If provided, write to file instead of stdout.

**Behavior:**
- If `output_path` is set, writes to file (appending `\n` if missing), otherwise `click.echo()` to stdout.

This replaces the four duplicated `_write()` functions.

---

#### `emit_error(reference: str, reason: str, exit_code: int = 1) -> None`

Print a standardized error message to stderr and exit.

**Parameters:**
- `reference` — Context identifier (image ref, registry, repo). Displayed in brackets.
- `reason` — Human-readable error description.
- `exit_code` — Process exit code (default: 1).

**Output format:**
```
Error [registry/repo:tag]: manifest not found
```

**Behavior:**
- `click.echo(f"Error [{reference}]: {reason}", err=True)`
- `sys.exit(exit_code)`

---

#### `emit_table(rows: list[list[str]], headers: list[str] | None = None) -> None`

Print tabular data with aligned columns.

**Parameters:**
- `rows` — List of row data, each row a list of string values.
- `headers` — Optional column headers. When provided, printed first with a separator line.

**Behavior:**
- Calculates column widths from the maximum length in each column (including headers).
- Left-aligns all columns with 2-space padding between them.
- Writes to stdout via `click.echo()`.

**Example output (with headers):**
```
DIGEST                            ARTIFACT TYPE                    SIZE
sha256:abc123...                  application/vnd.example+json     1024
sha256:def456...                  application/vnd.other+json       2048
```

**Example output (without headers):**
```
sha256:abc123...  application/vnd.example+json  1024
sha256:def456...  application/vnd.other+json    2048
```

---

#### `emit_list(items: list[str], output_path: str | None = None) -> None`

Print a simple one-item-per-line list.

**Parameters:**
- `items` — List of string items.
- `output_path` — If provided, write to file instead of stdout.

**Behavior:**
- Joins items with `"\n"` and emits via `emit_text()`.
- Replaces inline `"\n".join(...)` patterns in `tag list` and `catalog list`.

---

#### `format_key_value(pairs: list[tuple[str, str]], separator: str = ":") -> str`

Format aligned key-value pairs for display (used by `manifest info`).

**Parameters:**
- `pairs` — List of `(key, value)` tuples.
- `separator` — Character between key and value (default: `":"`).

**Returns:** Formatted multi-line string with right-aligned keys.

**Example:**
```
Digest:       sha256:abc123...
Media Type:   application/vnd.oci.image.manifest.v1+json
Size:         1234
```

---

#### `progress_status(message: str) -> None`

Print a transient status message to stderr for long-running operations.

**Parameters:**
- `message` — Status message to display.

**Behavior:**
- Writes to stderr (`err=True`) so it doesn't interfere with piped stdout.
- Uses `click.echo(message, err=True)`.

**Usage in CLI commands:**
```python
from regshape.cli.formatting import progress_status

progress_status("Uploading blob...")
result = upload_blob(client, repo, file_path)
progress_status("Upload complete.")
```

---

## Migration Plan

### Phase 1: Create `formatting.py` with all helpers

Create the module with the functions defined above. Add unit tests.

### Phase 2: Migrate existing CLI modules

Replace duplicated code in each CLI module one at a time:

| Module | Change |
|--------|--------|
| `manifest.py` | Replace `_write()` with `emit_text()` / `emit_json()`; replace `_emit_error()` with `emit_error()` |
| `tag.py` | Replace `_write()` with `emit_text()` / `emit_list()`; replace `_emit_error()` with `emit_error()` |
| `catalog.py` | Replace `_write()` with `emit_text()` / `emit_list()`; replace `_emit_error()` with `emit_error()` |
| `referrer.py` | Replace `_write()` with `emit_text()`; replace `_emit_error()` with `emit_error()`; use `emit_table()` for list output |
| `blob.py` | Replace `_emit_error()` with `emit_error()` |
| `ping.py` | Replace inline error echoing with `emit_error()` |
| `auth.py` | Replace `_emit_error()` with `emit_error()` |
| `layout.py` | Replace inline progress messages with `progress_status()`; replace `_emit_error()` with `emit_error()` |
| `docker.py` | Replace `_emit_error()` with `emit_error()` |

### Phase 3: Add tests

Create `src/regshape/tests/test_formatting.py` covering:

- `emit_json` — Verifies correct JSON structure and file output.
- `emit_text` — Verifies stdout and file output with newline handling.
- `emit_error` — Verifies stderr output format and `sys.exit()` call.
- `emit_table` — Verifies column alignment with and without headers.
- `emit_list` — Verifies newline-joined output.
- `format_key_value` — Verifies aligned key-value output.
- `progress_status` — Verifies output goes to stderr.

## Design Decisions

### Why plain functions, not a class?

The formatting helpers are stateless utilities. A class would add ceremony without benefit. Individual functions are easier to import selectively and test independently.

### Why no third-party table library (e.g., `tabulate`, `rich`)?

The project convention is to keep dependencies minimal (`click`, `requests`, `pytest` only). The table formatting needed is simple enough to implement with basic string operations. A third-party library can be considered later if requirements grow.

### Why `progress_status()` instead of spinners/progress bars?

Click does provide `click.progressbar()`, but the current blob upload and layout push operations don't expose a byte-level progress callback. Simple status messages to stderr are sufficient for now and don't require refactoring the operation layer. This can be enhanced later when streaming upload progress is available.

### Why `emit_error()` calls `sys.exit()`?

This matches the existing pattern where `_emit_error()` always exits. Keeping the exit in the helper reduces the chance of forgetting to exit after an error. Commands that need to handle errors without exiting can use `click.echo(..., err=True)` directly.

## Dependencies

- **Internal:** `click` (already a project dependency)
- **External:** None (no new dependencies)

## Open Questions

- [ ] Should `emit_table()` support right-aligned numeric columns? (Current proposal: all left-aligned for simplicity.)
- [ ] Should `progress_status()` use `\r` for overwriting the same line, or print sequential lines? (Current proposal: sequential lines.)
- [ ] Should `emit_error()` accept an optional `--json` flag to output errors as JSON objects? (Some tools do this for machine-parseable error reporting.)
